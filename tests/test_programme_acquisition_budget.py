"""Bounded cross-scope work; manufactured receipts and disposable stores only."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from threading import RLock
import unittest
from unittest.mock import patch

import test_dropship_platform_roles as roles_case
import test_programme
from product_radar.models import SafetyError, timestamp
from product_radar.programme import (_acquire, _acquisition_scopes, _register_discovered_products,
    _save, advance_programme, load_programme)
from product_radar.research_memory import ResearchMemory
from product_radar.source_coverage import ROUTES


class AcquisitionBudgetTests(unittest.TestCase):
    setUp = test_programme.ProgrammeTests.setUp
    product = test_programme.ProgrammeTests.product
    prepare = test_programme.ProgrammeTests.prepare
    noops = test_programme.ProgrammeTests.noops
    advance = test_programme.ProgrammeTests.advance

    def children(self, count):
        record = self.prepare()
        for n in range(count):
            cid = "child-" + str(n)
            self.product(cid)
            _register_discovered_products(self.memory, record, {"new_concept_ids": [cid]}, self.now)
        _save(self.memory, record)
        return record

    def spies(self, callback=None):
        calls = []
        def source(_memory, scope, context):
            calls.append((scope["programme_id"], context["source_limit"]))
            if callback:
                return callback(scope, context)
            return {"handler_calls": context["source_limit"], "fresh_dispatches": context["source_limit"]}
        steps = self.noops(); steps["acquire"] = source
        return steps, calls

    def actual(self, *, handlers=None, request_builder=None, source_limit=12):
        calls, reports = [], []
        def empty(context):
            context["record_dispatch"](); calls.append(context["acquisition_id"])
            return self.receipt(context)
        steps = self.noops(); steps.pop("acquire")
        steps["reports"] = lambda *_args: reports.append(len(calls)) or {}
        result = advance_programme(self.memory, "whole-programme", root=self.root,
            handlers=handlers or {route: empty for route in ROUTES},
            request_builder=request_builder or (lambda context: {"query": context["concept_id"] + " " + context["route"], "country": "IN"}),
            steps=steps, source_limit=source_limit, max_cycles=1, as_of=self.now)
        return result, calls, reports

    def receipt(self, context):
        return {"schema_version": 1, "route": context["route"], "acquisition_id": context["acquisition_id"],
            "observed_at": context["as_of"], "state": "empty", "dispatch_started": True,
            "reference": context["acquisition_id"] + "/receipt.json", "capture_ids": [], "observation_ids": [],
            "target_results": [], "reason": "Synthetic readable empty result.", "retryable": False}

    def test_five_follow_ons_share_twelve_actual_actions_before_reports(self):
        original = self.children(5)
        result, calls, reports = self.actual()
        record = load_programme(self.memory, "whole-programme")
        self.assertEqual(len(calls), 12)
        self.assertEqual(reports, [12])
        self.assertEqual(record["last_results"]["acquire"]["handler_calls"], 6)
        self.assertEqual(record["acquisition_budget"]["handler_calls"], 12)
        self.assertEqual(record["acquisition_budget"]["fresh_dispatches"], 12)
        self.assertEqual(record["acquisition_budget"]["callbacks_without_new_dispatch"], 0)
        self.assertEqual(len(record["acquisition_budget"]["selected_scope_ids"]), 2)
        self.assertEqual(record["concept_ids"], original["concept_ids"])
        self.assertEqual(record["source_run_id"], original["source_run_id"])
        self.assertEqual(self.memory.snapshot()["published_history"], {})
        self.assertTrue(result["report_filled_does_not_stop_work"])

    def test_unused_original_allowance_goes_to_the_selected_follow_on(self):
        self.children(3)
        steps, calls = self.spies(lambda scope, context: {"handler_calls": 2 if not scope.get("parent_programme_id") else context["source_limit"],
            "fresh_dispatches": 2 if not scope.get("parent_programme_id") else context["source_limit"]})
        self.advance(steps=steps, source_limit=12, max_cycles=1)
        self.assertEqual([quota for _, quota in calls], [6, 10])
        self.assertEqual(load_programme(self.memory, "whole-programme")["acquisition_budget"]["budget_units_consumed"], 12)

    def test_zero_callback_child_does_not_hide_next_ready_child(self):
        self.children(3); called = []
        record = load_programme(self.memory, "whole-programme")
        first_child = _acquisition_scopes(record, 12)[1][0]["programme_id"]
        _save(self.memory, record)
        def source(scope, context):
            called.append(scope["programme_id"])
            count = 0 if scope["programme_id"] == first_child else context["source_limit"]
            return {"handler_calls": count, "fresh_dispatches": count}
        steps, quotas = self.spies(source)
        self.advance(steps=steps, source_limit=12, max_cycles=1)
        self.assertEqual([quota for _, quota in quotas], [6, 6, 6])
        record = load_programme(self.memory, "whole-programme")
        self.assertEqual(record["acquisition_budget"]["handler_calls"], 12)
        self.assertEqual(len(set(called)), 3)

    def test_all_zero_children_return_unused_allowance_to_original(self):
        self.children(3)
        steps, calls = self.spies(lambda scope, context: {"handler_calls": 0 if scope.get("parent_programme_id") else context["source_limit"],
            "fresh_dispatches": 0 if scope.get("parent_programme_id") else context["source_limit"]})
        self.advance(steps=steps, source_limit=12, max_cycles=1)
        self.assertEqual([quota for _, quota in calls], [6, 6, 6, 6, 6])
        record = load_programme(self.memory, "whole-programme")
        self.assertEqual(record["last_results"]["acquire"]["handler_calls"], 12)
        self.assertEqual(record["acquisition_budget"]["handler_calls"], 12)
        self.assertEqual(len({scope for scope, _ in calls}), 4)

    def test_empty_rotations_do_not_make_an_infinite_progress_loop(self):
        self.children(4)
        steps, calls = self.spies(lambda *_args: {"handler_calls": 0, "fresh_dispatches": 0})
        result = self.advance(steps=steps, source_limit=12, max_cycles=10)
        self.assertEqual(result["cycles_this_tick"], 1)
        self.assertEqual(result["tick_state"], "fixed_point_with_saved_remaining_work")
        self.assertEqual(len(calls), 5)
        budget = load_programme(self.memory, "whole-programme")["acquisition_budget"]
        self.assertEqual(budget["handler_calls"], 0)
        self.assertEqual(budget["fresh_dispatches"], 0)

    def test_repeated_empty_rotations_cannot_reuse_an_old_control_event(self):
        self.children(3)
        steps, calls = self.spies(lambda *_args: {"handler_calls": 0, "fresh_dispatches": 0})
        sweeps = []
        for _ in range(4):
            result = self.advance(steps=steps, source_limit=12, max_cycles=10)
            self.assertEqual(result["cycles_this_tick"], 1)
            sweeps.append(load_programme(self.memory, "whole-programme")["acquisition_sweep"])
        self.assertEqual(len(calls), 16)
        self.assertTrue(all(new > old for old, new in zip(sweeps, sweeps[1:])))

    def test_limit_one_alternates_original_and_rotating_children(self):
        self.children(3); steps, calls = self.spies()
        for _ in range(6):
            self.advance(steps=steps, source_limit=1, max_cycles=1)
        original_calls = [scope for scope, _ in calls if scope == "whole-programme"]
        children = [scope for scope, _ in calls if scope != "whole-programme"]
        self.assertEqual(len(original_calls), 3)
        self.assertEqual(len(set(children)), 3)
        self.assertEqual([scope for scope, _ in calls][::2], ["whole-programme"] * 3)
        self.assertTrue(all(quota == 1 for _, quota in calls))

    def test_limit_one_empty_original_passes_allowance_to_a_ready_child(self):
        self.children(2)
        steps, calls = self.spies(lambda scope, context: {
            "handler_calls": 1 if scope.get("parent_programme_id") else 0,
            "fresh_dispatches": 1 if scope.get("parent_programme_id") else 0})
        self.advance(steps=steps, source_limit=1, max_cycles=1)
        self.assertEqual([quota for _, quota in calls], [1, 1])
        self.assertEqual(load_programme(self.memory, "whole-programme")["acquisition_budget"]["handler_calls"], 1)

    def test_constant_admissions_do_not_keep_old_children_behind_new_arrivals(self):
        self.children(3); steps, calls = self.spies()
        for n in range(12):
            cid = "admission-" + str(n); self.product(cid)
            self.advance(steps=steps, source_limit=12, max_cycles=1,
                discovery_step=lambda *_args, cid=cid: {"state": "checked", "new_concept_ids": [cid]})
        record = load_programme(self.memory, "whole-programme")
        old_ids = {child["programme_id"] for child in record["follow_on_reviews"].values()
                   if child["concept_ids"][0].startswith("child-")}
        served = [scope for scope, _ in calls if scope != "whole-programme"]
        self.assertTrue(all(served.count(scope) >= 2 for scope in old_ids))
        self.assertEqual(len([scope for scope, _ in calls if scope == "whole-programme"]), 12)
        self.assertEqual(len(set(child["acquisition_ordinal"] for child in record["follow_on_reviews"].values())), 15)

    def test_rotation_survives_cold_memory_reload_without_changing_bindings(self):
        self.children(3); steps, calls = self.spies()
        self.advance(steps=steps, source_limit=12, max_cycles=1)
        before = load_programme(self.memory, "whole-programme")
        self.memory = ResearchMemory(self.memory.root, self.memory.journal, evidence_type="synthetic")
        self.advance(steps=steps, source_limit=12, max_cycles=1)
        after = load_programme(self.memory, "whole-programme")
        self.assertNotEqual(calls[1][0], calls[3][0])
        self.assertEqual(before["concept_ids"], after["concept_ids"])
        self.assertEqual(before["source_run_id"], after["source_run_id"])
        self.assertEqual(set(before["follow_on_reviews"]), set(after["follow_on_reviews"]))

    def test_lost_local_result_reserves_quota_and_healthy_follow_on_continues(self):
        self.children(2)
        def source(scope, context):
            if not scope.get("parent_programme_id"):
                raise OSError("Synthetic lost local source return")
            return {"handler_calls": context["source_limit"], "fresh_dispatches": context["source_limit"]}
        steps, calls = self.spies(source)
        self.advance(steps=steps, source_limit=12, max_cycles=1)
        record = load_programme(self.memory, "whole-programme")
        self.assertEqual([quota for _, quota in calls], [6, 6])
        self.assertEqual(record["acquisition_budget"]["budget_units_consumed"], 12)
        self.assertIsNone(record["acquisition_budget"]["handler_calls"])
        self.assertIsNone(record["acquisition_budget"]["fresh_dispatches"])
        self.assertEqual(record["last_results"]["acquire"]["reason"], "PROGRAMME_LOCAL_FILE_FAILED")

    def test_shared_capture_replay_is_a_callback_not_a_second_fresh_dispatch(self):
        self.children(1); new, saved = [], []
        def collect(context):
            new.append(context["acquisition_id"]); context["record_dispatch"]()
            return self.receipt(context)
        def reconcile(context):
            saved.append(context["acquisition_id"])
            return self.receipt(context)
        builder = lambda context: ({"query": "Sink caddy", "country": "IN"}
            if context["route"] == "meta-ads" else {"not_applicable_reason": "Outside this synthetic focused route."})
        self.actual(handlers={"meta-ads": {"handler": collect, "reconcile": reconcile}}, request_builder=builder)
        record = load_programme(self.memory, "whole-programme")
        self.assertEqual(len(new), 1)
        self.assertEqual(len(saved), 1)
        self.assertEqual(new, saved)
        self.assertEqual(record["acquisition_budget"]["handler_calls"], 2)
        self.assertEqual(record["acquisition_budget"]["fresh_dispatches"], 1)
        self.assertEqual(record["acquisition_budget"]["callbacks_without_new_dispatch"], 1)
        child = next(iter(record["follow_on_reviews"].values()))
        self.assertEqual(child["last_results"]["acquire"]["fresh_dispatches"], 0)

    def test_prior_uncertain_reply_recovery_is_not_a_new_dispatch(self):
        self.prepare()
        def timeout(context):
            raise TimeoutError("Synthetic prior read with unknown dispatch receipt")
        builder = lambda context: ({"query": "same focused request", "country": "IN"}
            if context["route"] == "meta-ads" else {"not_applicable_reason": "Outside synthetic focused route."})
        self.actual(handlers={"meta-ads": timeout}, request_builder=builder)
        result, _, _ = self.actual(handlers={"meta-ads": {"handler": timeout, "reconcile": self.receipt}}, request_builder=builder)
        record = load_programme(self.memory, "whole-programme")
        self.assertEqual(record["acquisition_budget"]["handler_calls"], 1)
        self.assertEqual(record["acquisition_budget"]["fresh_dispatches"], 0)
        self.assertEqual(record["acquisition_budget"]["callbacks_without_new_dispatch"], 1)

    def test_recovered_old_failure_then_new_retry_counts_only_the_new_dispatch(self):
        from product_radar.source_coverage import execute_source_run, prepare_all_source_tasks
        self.prepare(); old = (timestamp(self.now) - timedelta(hours=1)).isoformat()
        builder = lambda context: ({"query": "one focused family", "country": "IN"}
            if context["route"] == "meta-ads" else {"not_applicable_reason": "Outside synthetic focused route."})
        prepare_all_source_tasks(self.memory, run_id="existing-source-run", request_builder=builder,
            concept_ids=["first", "second"], as_of=old)
        def lost(context):
            context["record_dispatch"]()
            raise TimeoutError("Synthetic original result lost")
        with patch("product_radar.source_coverage.utc_now", return_value=old):
            execute_source_run(self.memory, "existing-source-run", handlers={"meta-ads": lost}, as_of=old,
                bounded_call=lambda callback, context, _timeout: callback(context))
        recovered, fresh = [], []
        def old_failed(context):
            recovered.append(context["acquisition_id"])
            return dict(self.receipt(context), state="failed", retryable=True, observed_at=old)
        def collect(context):
            fresh.append(context["acquisition_id"]); context["record_dispatch"]()
            return self.receipt(context)
        self.actual(handlers={"meta-ads": {"handler": collect, "reconcile": old_failed}}, request_builder=builder)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(len(fresh), 1)
        self.assertNotEqual(recovered, fresh)
        budget = load_programme(self.memory, "whole-programme")["acquisition_budget"]
        self.assertEqual(budget["handler_calls"], 2)
        self.assertEqual(budget["fresh_dispatches"], 1)
        self.assertEqual(budget["callbacks_without_new_dispatch"], 1)

    def test_installed_terminal_supplier_recovery_is_not_a_new_dispatch(self):
        from product_radar.source_coverage import execute_source_run, prepare_all_source_tasks
        from product_radar.storage import atomic_json
        from product_radar.supplier_route_acquisition import create_supplier_handlers
        from test_supplier_route_acquisition import QUERY, receipt as supplier_receipt
        self.prepare(); calls, requests = [], []
        def primary(item):
            calls.append("http")
            return supplier_receipt(item, status=500, reason="PUBLIC_FETCH_DEADLINE", actual=False)
        def browser(item):
            calls.append("browser"); requests.append(deepcopy(item))
            folder = self.memory.root / "supplier_routes/native" / item["id"]
            folder.mkdir(parents=True)
            return dict(supplier_receipt(item, status=500, reason="SUPPLIER_NATIVE_READ_FAILED", actual=False),
                native_reference=str(folder / "native_result.json"), native_reason="NATIVE_PUBLIC_EXECUTION_FAILED",
                native_stage="result_read", checked_at=self.now)
        handlers = create_supplier_handlers(self.memory, http_reader=primary, browser_reader=browser)
        builder = lambda context: ({"query": QUERY} if context["route"] == "tradeindia"
            else {"not_applicable_reason": "Outside this synthetic supplier recovery."})
        prepare_all_source_tasks(self.memory, run_id="existing-source-run", request_builder=builder,
            concept_ids=["first", "second"], as_of=self.now)
        execute_source_run(self.memory, "existing-source-run", handlers=handlers, as_of=self.now,
            action_limit=1, bounded_call=lambda callback, context, _timeout: callback(context))
        task_id, old = next((task_id, deepcopy(task)) for task_id, task in self.memory.snapshot()["query_tasks"].items()
            if task["route"] == "tradeindia")
        self.assertEqual(old["tries"], 0); self.assertFalse(old["reconcile_required"])
        self.assertNotIn("original_dispatch_recovery", old)
        capture_path = self.memory.root / "supplier_routes" / old["acquisition_id"] / "capture.json"
        original = capture_path.read_bytes()
        item = requests[0]
        atomic_json(self.memory.root / "supplier_routes/native" / item["id"] / "native_dispatch.json",
            {"url": item["url"], "navigation_started": True, "navigation_started_at": self.now})
        self.actual(handlers=handlers, request_builder=builder)
        budget = load_programme(self.memory, "whole-programme")["acquisition_budget"]
        self.assertEqual(budget["handler_calls"], 1)
        self.assertEqual(budget["fresh_dispatches"], 0)
        self.assertEqual(budget["callbacks_without_new_dispatch"], 1)
        current = self.memory.snapshot()["query_tasks"][task_id]
        self.assertEqual(current["acquisition_id"], old["acquisition_id"])
        self.assertEqual(current["generation"], old["generation"])
        self.assertEqual(current["tries"], 1)
        self.assertIs(current["dispatch_started"], True)
        self.assertEqual(calls, ["http", "browser"])
        self.assertEqual(capture_path.read_bytes(), original)
        self.actual(handlers=handlers, request_builder=builder)
        self.assertEqual(load_programme(self.memory, "whole-programme")["acquisition_budget"]["handler_calls"], 0)
        self.assertEqual(calls, ["http", "browser"])

    def test_terminal_marker_exempts_only_exact_original_id_present_before_callback(self):
        old = {"run_id": "one-run", "acquisition_id": "original-acquisition", "request_hash": "original-request",
               "attempt_refs": [], "status": "failed", "reconcile_required": False}
        marker = {"version": "supplier-original-dispatch-recovery-v1", "acquisition_id": "original-acquisition",
                  "request_hash": "original-request"}
        cases = [
            ("newly recorded valid marker", "task", "task", old, marker, ["original-acquisition"], "original-request", 0),
            ("new generation still counts", "task", "task", old, marker,
             ["original-acquisition", "new-generation"], "original-request", 1),
            ("wrong version", "task", "task", old, dict(marker, version="unrecognised"), ["original-acquisition"], "original-request", 1),
            ("wrong acquisition", "task", "task", old, dict(marker, acquisition_id="other-acquisition"), ["original-acquisition"], "original-request", 1),
            ("wrong request", "task", "task", old, dict(marker, request_hash="other-request"), ["original-acquisition"], "original-request", 1),
            ("changed task request", "task", "task", old, marker, ["original-acquisition"], "changed-request", 1),
            ("foreign task", "old-task", "new-task", old, marker, ["original-acquisition"], "original-request", 1),
            ("new acquisition not present before", "task", "task", dict(old, acquisition_id=None), marker,
             ["original-acquisition"], "original-request", 1),
        ]
        for name, old_id, new_id, before, recovery, refs, request_hash, expected in cases:
            with self.subTest(name=name):
                after = dict(before, request_hash=request_hash, acquisition_id=refs[-1], attempt_refs=refs,
                    original_dispatch_recovery=recovery)
                snapshots = iter([{"query_tasks": {old_id: before}}, {"query_tasks": {new_id: after}}])
                # The production memory supplies this lock for its context-local
                # optional turn binding. Keep this narrow accounting double
                # faithful without disabling that real execution check.
                memory = SimpleNamespace(snapshot=lambda: next(snapshots), _cache_lock=RLock())
                with patch("product_radar.source_coverage.continue_source_run", return_value={"handler_calls": 2}):
                    result = _acquire(memory, {"source_run_id": "one-run", "concept_ids": []},
                        {"request_builder": None, "handlers": {}, "source_limit": 2, "retry_seconds": 60})
                self.assertEqual(result["fresh_dispatches"], expected)
                self.assertEqual(result["callbacks_without_new_dispatch"], 2 - expected)

    def saved_answer_run(self, *, with_callback_route):
        """Dropship per-type questions answered from the saved catalogue (live 16 Oct)."""
        self.prepare(); handler_hits, fresh = [], []
        def saved(context):
            # As platform_matching: the saved local answer comes before any
            # callback admission, and its receipt says dispatch_started because
            # the catalogue reads happened earlier, in their own lane.
            return {"action": "saved", "acquisition_id": context["acquisition_id"], "receipt": self.receipt(context)}
        def never(context):
            handler_hits.append(context["acquisition_id"])
            raise AssertionError("A saved local answer must not call its handler")
        def collect(context):
            fresh.append(context["acquisition_id"]); context["record_dispatch"]()
            return self.receipt(context)
        routes = {"baapstore", "meta-ads"} if with_callback_route else {"baapstore"}
        builder = lambda context: ({"query": "Sink caddy " + context["concept_id"], "country": "IN"}
            if context["route"] in routes else {"not_applicable_reason": "Outside this synthetic focused route."})
        handlers = {"baapstore": {"handler": never, "reconcile": never, "source_access_preflight": saved}}
        if with_callback_route:
            handlers["meta-ads"] = collect
        self.actual(handlers=handlers, request_builder=builder)
        record = load_programme(self.memory, "whole-programme")
        tasks = [task for task in self.memory.snapshot()["query_tasks"].values() if task["route"] == "baapstore"]
        self.assertEqual(handler_hits, [])
        self.assertEqual(len(tasks), 2)
        for task in tasks:
            # The answer is applied and kept as a real attempt in the record.
            self.assertEqual(task["attempt_refs"], [task["acquisition_id"]])
            self.assertFalse(task["reconcile_required"])
        self.assertNotIn("reason", record["last_results"]["acquire"])
        return record, fresh, sorted(task["acquisition_id"] for task in tasks)

    def test_saved_local_answers_alone_are_not_fresh_dispatches(self):
        record, fresh, answered = self.saved_answer_run(with_callback_route=False)
        self.assertEqual(fresh, [])
        budget = record["acquisition_budget"]
        self.assertEqual(budget["handler_calls"], 0)
        self.assertEqual(budget["fresh_dispatches"], 0)
        self.assertEqual(budget["callbacks_without_new_dispatch"], 0)
        self.assertEqual(budget["budget_units_consumed"], 0)
        result = record["last_results"]["acquire"]
        self.assertEqual((result["handler_calls"], result["fresh_dispatches"]), (0, 0))
        self.assertEqual(result["saved_local_answers"], len(answered))
        self.assertNotIn("saved_answer_refs", result)  # the record keeps only the count

    def test_saved_local_answers_beside_real_callbacks_count_only_the_callbacks(self):
        record, fresh, answered = self.saved_answer_run(with_callback_route=True)
        self.assertEqual(len(fresh), 2)
        self.assertFalse(set(fresh) & set(answered))
        budget = record["acquisition_budget"]
        self.assertEqual(budget["handler_calls"], 2)
        self.assertEqual(budget["fresh_dispatches"], 2)
        self.assertEqual(budget["callbacks_without_new_dispatch"], 0)
        self.assertEqual(budget["budget_units_consumed"], 2)
        self.assertEqual(record["last_results"]["acquire"]["saved_local_answers"], 2)

    def test_saved_answer_refs_exempt_only_their_own_new_attempts(self):
        before = {"task": {"run_id": "one-run", "attempt_refs": [], "status": "prepared"},
                  "other": {"run_id": "one-run", "attempt_refs": [], "status": "prepared"}}
        after = {"task": dict(before["task"], status="acquired", acquisition_id="saved-answer",
                              attempt_refs=["saved-answer"]),
                 "other": dict(before["other"], status="acquired", acquisition_id="real-read",
                               attempt_refs=["real-read"])}
        def run(returned):
            snapshots = iter([{"query_tasks": before}, {"query_tasks": after}])
            memory = SimpleNamespace(snapshot=lambda: next(snapshots), _cache_lock=RLock())
            with patch("product_radar.source_coverage.continue_source_run", return_value=returned):
                return _acquire(memory, {"source_run_id": "one-run", "concept_ids": []},
                    {"request_builder": None, "handlers": {}, "source_limit": 2, "retry_seconds": 60})
        cases = [("saved answer exempts only itself", {"handler_calls": 1, "saved_answer_refs": ["saved-answer"]}, 1, 1),
                 ("no list counts every new attempt", {"handler_calls": 2}, 2, 0),
                 ("unrelated saved ref changes nothing", {"handler_calls": 2, "saved_answer_refs": ["elsewhere"]}, 2, 1)]
        for name, returned, expected, saved in cases:
            with self.subTest(name=name):
                result = run(returned)
                self.assertEqual(result["fresh_dispatches"], expected)
                self.assertEqual(result["callbacks_without_new_dispatch"], returned["handler_calls"] - expected)
                self.assertEqual(result["saved_local_answers"], saved)
                self.assertNotIn("saved_answer_refs", result)
        for bad in ("saved-answer", ["saved-answer", 7], {"saved-answer"}, None):
            with self.subTest(bad=bad), self.assertRaisesRegex(SafetyError, "PROGRAMME_ACQUISITION_COUNTS_INVALID"):
                run({"handler_calls": 1, "saved_answer_refs": bad})

    def test_bad_cursor_or_duplicate_ordinal_is_not_silently_accepted(self):
        record = self.children(2)
        for cursor in ({"last": True, "ceiling": 2}, {"last": -1, "ceiling": 2}, {"last": 1, "ceiling": 0}):
            bad = deepcopy(record); bad["acquisition_cursor"] = cursor
            with self.assertRaisesRegex(SafetyError, "PROGRAMME_CONTROL_INVALID"):
                _acquisition_scopes(bad, 12)
        bad = deepcopy(record)
        for child in bad["follow_on_reviews"].values():
            child["acquisition_ordinal"] = 1
        with self.assertRaisesRegex(SafetyError, "PROGRAMME_CONTROL_INVALID"):
            _acquisition_scopes(bad, 12)
        for value in (True, -1, "1"):
            bad = deepcopy(record); bad["acquisition_sweep"] = value
            with self.assertRaisesRegex(SafetyError, "PROGRAMME_CONTROL_INVALID"):
                _acquisition_scopes(bad, 12)

    def test_invalid_callback_counts_stay_unknown_not_false_zero_or_progress(self):
        for values in ({"handler_calls": True}, {"handler_calls": -1},
                       {"handler_calls": 13}, {"handler_calls": 1, "fresh_dispatches": True},
                       {"handler_calls": 1, "fresh_dispatches": 2}, {"fresh_dispatches": True},
                       {"fresh_dispatches": 1}, {"handler_calls": None, "fresh_dispatches": 0}):
            with self.subTest(values=values):
                record = self.prepare()
                record["deferrals"].clear(); _save(self.memory, record)
                steps = self.noops(); steps["acquire"] = lambda *_args: values
                self.advance(steps=steps, source_limit=12, max_cycles=1)
                saved = load_programme(self.memory, "whole-programme")
                self.assertEqual(saved["last_results"]["acquire"]["reason"], "PROGRAMME_ACQUISITION_COUNTS_INVALID")
                self.assertEqual(saved["acquisition_budget"]["budget_units_consumed"], 12)
                self.assertIsNone(saved["acquisition_budget"]["handler_calls"])
                self.assertIsNone(saved["acquisition_budget"]["fresh_dispatches"])

    def test_real_clock_refreshes_each_later_scope_after_slow_source_capture(self):
        self.children(1)
        start = self.now; capture_at = (timestamp(start) + timedelta(minutes=40)).isoformat()
        clock_value = {"at": start}; seen = []
        steps = self.noops()
        def acquire(_memory, scope, context):
            seen.append(("acquire", scope["programme_id"], context["at"]))
            clock_value["at"] = capture_at
            return {"handler_calls": 0, "fresh_dispatches": 0}
        steps["acquire"] = acquire; steps.pop("match"); steps.pop("suppliers")
        def matched(_memory, _runner, **options):
            seen.append(("match", options["run_id"], options["as_of"]))
            return {"outcomes": [], "reused": []}
        def supplied(_memory, _runner, **options):
            self.assertIsNone(options["as_of"])
            seen.append(("suppliers", "saved-supplier-inputs", clock_value["at"]))
            return {"selected_capture_ids": [], "selected_inputs": [], "accepted": {}, "failures": {},
                    "reused_capture_ids": [], "remaining_inputs": 0, "source_bindings": [], "skipped_captures": []}
        steps["reports"] = lambda _m, _r, context: seen.append(("reports", "shared-reports", context["at"])) or {}
        with patch("product_radar.source_interpretation.review_source_results", side_effect=matched), \
             patch("product_radar.source_interpretation.review_supplier_results", side_effect=supplied):
            advance_programme(self.memory, "whole-programme", root=self.root, handlers={}, steps=steps,
                source_limit=12, max_cycles=1, clock=lambda: clock_value["at"])
        self.assertEqual(seen[0][2], start)
        self.assertTrue(all(timestamp(at) >= timestamp(capture_at) for stage, _, at in seen if stage != "acquire"))
        child = next(iter(load_programme(self.memory, "whole-programme")["follow_on_reviews"].values()))
        self.assertEqual(child["updated_at"], capture_at)
        self.assertEqual(child["last_results"]["suppliers"]["remaining_inputs"], 0)

    def test_supplier_live_cutoff_differs_from_explicit_and_standalone_modes(self):
        from product_radar.programme import _suppliers
        record = self.prepare()
        context = {"at": self.now, "runner": None, "supplier_limit": 5}
        response = {"selected_capture_ids": [], "selected_inputs": [], "accepted": {}, "failures": {},
                    "reused_capture_ids": [], "remaining_inputs": 0, "source_bindings": [], "skipped_captures": []}
        with patch("product_radar.source_interpretation.review_supplier_results", return_value=response) as supplied:
            _suppliers(self.memory, record, dict(context, clock=lambda: self.now, fixed_as_of=None))
            self.assertIsNone(supplied.call_args.kwargs["as_of"])
            _suppliers(self.memory, record, dict(context, clock=lambda: self.now, fixed_as_of=self.now))
            self.assertEqual(supplied.call_args.kwargs["as_of"], self.now)
            _suppliers(self.memory, record, context)
            self.assertEqual(supplied.call_args.kwargs["as_of"], self.now)

    def test_failure_wait_and_model_pause_use_actual_failure_time(self):
        self.prepare(); start = self.now
        failed_at = (timestamp(start) + timedelta(hours=1)).isoformat()
        clock_value = {"at": start}
        def runner(*_args):
            clock_value["at"] = failed_at
            raise SafetyError("AI_NETWORK_UNAVAILABLE")
        steps = self.noops()
        steps["fit"] = lambda _m, _r, context: context["runner"]("E", {"items": [{"id": "first"}]}, self.root)
        advance_programme(self.memory, "whole-programme", root=self.root, handlers={}, steps=steps,
            runner=runner, max_cycles=1, clock=lambda: clock_value["at"], retry_seconds=300)
        record = load_programme(self.memory, "whole-programme")
        self.assertEqual(record["model_pause"]["at"], failed_at)
        self.assertEqual(record["deferrals"]["lane:fit"]["at"], failed_at)
        self.assertEqual(timestamp(record["model_pause"]["next_retry_at"]) - timestamp(failed_at), timedelta(seconds=300))
        self.assertEqual(timestamp(record["deferrals"]["lane:fit"]["next_retry_at"]) - timestamp(failed_at), timedelta(seconds=300))

    def test_pause_expiring_during_public_work_allows_later_model_stage(self):
        self.prepare(); clock_value = {"at": self.now}; calls = []; seen = []
        later = (timestamp(self.now) + timedelta(seconds=301)).isoformat()
        def runner(role, *_args):
            calls.append(role)
            if role == "C":
                raise SafetyError("AI_NETWORK_UNAVAILABLE")
            return {}
        steps = self.noops()
        steps["profiles"] = lambda _m, _r, context: context["runner"]("C", {"items": []}, self.root)
        steps["fit"] = lambda _m, _r, context: seen.append(context["runner"]) or {}
        def acquire(*_args):
            clock_value["at"] = later
            return {"handler_calls": 0, "fresh_dispatches": 0}
        steps["acquire"] = acquire
        steps["match"] = lambda _m, _r, context: context["runner"]("E", {"items": []}, self.root)
        advance_programme(self.memory, "whole-programme", root=self.root, handlers={}, steps=steps,
            runner=runner, max_cycles=1, clock=lambda: clock_value["at"], retry_seconds=300)
        self.assertEqual(seen, [None])
        self.assertEqual(calls, ["C", "E"])
        self.assertIsNone(load_programme(self.memory, "whole-programme")["model_pause"])

    def test_explicit_as_of_remains_fixed_despite_slow_clock(self):
        self.prepare(); seen = []
        later = (timestamp(self.now) + timedelta(hours=1)).isoformat()
        steps = self.noops()
        def failed(_m, _r, context):
            seen.append(context["at"])
            raise SafetyError("SYNTHETIC_ROUTE_WAIT")
        steps["acquire"] = failed
        self.advance(steps=steps, max_cycles=1, clock=lambda: later)
        record = load_programme(self.memory, "whole-programme")
        self.assertEqual(seen, [self.now])
        self.assertEqual(record["deferrals"]["lane:acquire"]["at"], self.now)
        self.assertEqual(timestamp(record["deferrals"]["lane:acquire"]["next_retry_at"]) - timestamp(self.now), timedelta(seconds=300))


class InstalledDropshipAnswerAccountingTests(unittest.TestCase):
    """The installed per-type platform match through the programme's accounting.

    Live 16 Oct: every acquire pass that applied a DeoDap/BaapStore match
    failed PROGRAMME_ACQUISITION_COUNTS_INVALID (a saved answer counted as a
    fresh dispatch while no callback was made). Fixtures are borrowed from the
    platform roles tests by module, so their own cases are not collected twice.
    """
    setUpClass = classmethod(roles_case.RolesCase.setUpClass.__func__)
    tearDownClass = classmethod(roles_case.RolesCase.tearDownClass.__func__)
    setUp = roles_case.RolesCase.setUp
    build_catalogue = roles_case.RolesCase.build_catalogue
    request = roles_case.RolesCase.request
    task = roles_case.RolesCase.task

    def test_installed_platform_match_is_a_saved_answer_not_a_fresh_dispatch(self):
        from product_radar import platform_matching
        from product_radar.source_coverage import execute_source_run, prepare_source_tasks
        self.build_catalogue()
        prepare_source_tasks(self.memory, [{"route": "deodap", "request": self.request(), "concept_ids": [self.cid]}],
                             run_id="platform-run", as_of=roles_case.AT)
        handlers = platform_matching.create_dropship_handlers(self.memory)
        passes = []
        def one_pass(memory, run_id, **options):
            # The real dispatcher and installed handlers, in the wrapper's shape.
            result = execute_source_run(memory, run_id, handlers=handlers, as_of=roles_case.AT)
            passes.append(result)
            return {"handler_calls": result["handler_calls"], "saved_answer_refs": result["saved_answer_refs"]}
        with patch("product_radar.source_coverage.continue_source_run", side_effect=one_pass):
            result = _acquire(self.memory, {"source_run_id": "platform-run", "concept_ids": [self.cid]},
                {"request_builder": None, "handlers": handlers, "source_limit": 4, "retry_seconds": 300})
        task = self.task()
        self.assertEqual(passes[0]["results"][0]["state"], "acquired")
        self.assertEqual(passes[0]["saved_answer_refs"], [task["acquisition_id"]])
        self.assertEqual(task["attempt_refs"], [task["acquisition_id"]])  # still a real attempt in the record
        self.assertEqual((result["handler_calls"], result["fresh_dispatches"], result["saved_local_answers"]), (0, 0, 1))
        self.assertEqual(result["callbacks_without_new_dispatch"], 0)


if __name__ == "__main__":
    unittest.main()
