"""Actual installed Meta leaf, synthetic transport, real shared journal/callers."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar.catalogue_discovery import advance_discovery_cycle, prepare_next_discovery_wave
from product_radar.meta_browser import meta_direct_source_handlers, prepare_meta_query
from product_radar.models import SafetyError
from product_radar.research_memory import ResearchMemory
from product_radar.source_acquisitions import ACQUISITION_KIND
from product_radar.source_coverage import execute_source_run, prepare_source_tasks, source_access_scope, source_access_wait
from product_radar.storage import Journal
from product_radar.work_turns import active_work_turn, bind_work_turn, close_work_turn, open_work_turn
from test_catalogue_expansion import fixture
from test_shared_marketplace_acquisitions import wall_clock

NOW = "2026-10-09T10:00:00+00:00"


class SharedMetaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.calls = []; self.mode = "usable"; self.now = NOW
        self.sink = {"status": "active", "description": {"label": "Sink caddy",
            "category": "Home and Kitchen", "function": "Store sink essentials", "mechanism": "manual",
            "audience": "adult", "features": ["draining"], "brand": "generic_or_small",
            "basis": "observed", "reason": "Synthetic source fixture."}}
        self.memory.put("concepts", "sink", deepcopy(self.sink))

    def browser(self, url, expression, folder, **options):
        self.calls.append((url, str(folder)))
        if self.mode == "unknown": raise TimeoutError("Actual synthetic original interrupted")
        snapshot = {"url": url, "title": "Meta Ad Library", "text": "Ad Library", "text_truncated": False,
            "cards": [{"ad_id": "123456", "text": "Active\nLibrary ID: 123456\nStarted running on Oct 1, 2026\nSink caddy",
                       "links": [], "images": []}]}
        if self.mode in {"empty", "truncated", "blocked"}:
            snapshot.update(cards=[], text="Too many requests" if self.mode == "blocked" else "No ads match this search")
            snapshot["text_truncated"] = self.mode == "truncated"
        if self.mode == "scope_mismatch": snapshot["url"] = prepare_meta_query("other query")["url"]
        return {"snapshot": snapshot, "navigation_started": True, "navigation_started_at": self.now,
                "navigation_status": 429 if self.mode == "blocked" else 200, "owned_process_exited": True,
                "completed_at": self.now, "navigation_retry_after": "3600" if self.mode == "blocked" else None}

    def handlers(self, shared=True):
        return {"meta-ads": meta_direct_source_handlers(self.memory, browser=self.browser, shared_acquisitions=shared)}

    def prepare(self, handlers, *, query="sink caddy", run="checks"):
        self.run = run
        prepare_source_tasks(self.memory, [{"route": "meta-ads", "request": {"query": query, "country": "IN"},
            "concept_ids": ["sink"]}], run_id=run, as_of=self.now)
        self.wave = prepare_next_discovery_wave(self.memory, "programme", fixture(), handlers=handlers,
            as_of=self.now)

    def checks(self, handlers, **options):
        return execute_source_run(self.memory, self.run, handlers=handlers, as_of=self.now,
            bounded_call=lambda callback, context, timeout: callback(context), shared_marketplace=True, **options)

    def discovery(self, handlers):
        def advance():
            return advance_discovery_cycle(self.memory, self.wave["id"], handlers=handlers, as_of=self.now,
                admission=lambda *_args, **_options: None, shared_marketplace=True)
        if active_work_turn(self.memory) is not None: return advance()
        turn = open_work_turn(self.memory, "programme", at=self.now)
        try:
            with bind_work_turn(self.memory, turn["id"]): return advance()
        finally:
            close_work_turn(self.memory, turn["id"], at=self.now, reason="Synthetic finite Meta discovery")

    def owners(self):
        return {rid: row for rid, row in self.memory.snapshot()["batch_runs"].items() if row.get("kind") == ACQUISITION_KIND}

    def test_delayed_completion_uses_actual_time_across_iso_offsets(self):
        self.now = "2026-10-09T15:30:00+05:30"
        completed, committed = "2026-10-09T10:00:03+00:00", "2026-10-09T10:00:05+00:00"
        self.mode = "blocked"
        def delayed_browser(*args, **kwargs):
            return dict(self.browser(*args, **kwargs), completed_at=completed)
        handlers = {"meta-ads": meta_direct_source_handlers(self.memory, browser=delayed_browser,
                                                           shared_acquisitions=True)}
        self.prepare(handlers)
        with patch("product_radar.meta_browser.utc_now", return_value=committed):
            result = self.checks(handlers)
        self.assertEqual(result["counts"], {"blocked": 1})
        state = self.memory.snapshot(); owner = next(iter(self.owners().values()))
        response = state["source_checks"][owner["active_response_id"]]
        self.assertEqual(response["observed_at"], completed)
        self.assertEqual(response["recorded_at"], committed)
        failures = [row for row in state["source_checks"].values() if row.get("kind") == "source-access-refusal-v1"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["binding"]["observed_at"], completed)
        self.assertEqual(len(self.calls), 1)

    def test_native_subsecond_completion_is_compared_with_the_actual_commit_instant(self):
        for completed, counts in (("2026-10-09T10:00:05.6070928Z", {"acquired": 1}),
                                  ("2026-10-09T10:00:05.9000000Z", {"blocked": 1})):
            with self.subTest(completed=completed):
                self.memory = ResearchMemory(self.root / completed[-9:-1], Journal(self.root / (completed[-9:-1] + "-money")),
                                             evidence_type="synthetic")
                self.memory.put("concepts", "sink", deepcopy(self.sink))
                def native_browser(*args, **kwargs):
                    return dict(self.browser(*args, **kwargs), completed_at=completed)
                handlers = {"meta-ads": meta_direct_source_handlers(self.memory, browser=native_browser,
                                                                   shared_acquisitions=True)}
                self.prepare(handlers)
                with patch("product_radar.meta_browser.utc_now", wall_clock("2026-10-09T10:00:05.800000+00:00")):
                    result = self.checks(handlers)
                self.assertEqual(result["counts"], counts)
                owner = next(iter(self.owners().values()))
                if counts == {"acquired": 1}:
                    response = self.memory.snapshot()["source_checks"][owner["active_response_id"]]
                    self.assertEqual(response["observed_at"], completed)
                    self.assertEqual(response["recorded_at"], "2026-10-09T10:00:05.800000+00:00")
                else:  # A receipt dated after the actual commit instant still fails closed.
                    task = next(row for row in self.memory.snapshot()["query_tasks"].values()
                                if row.get("kind") == "all_source_task_v1" and row["run_id"] == self.run)
                    self.assertEqual(task["reason"], "SOURCE_ACQUISITION_SOURCE_DATE_INVALID")
                    self.assertIsNone(owner["active_response_id"])

    def test_direction_first_then_real_product_check_reuses_original(self):
        handlers = self.handlers(); self.prepare(handlers)
        self.assertEqual(self.discovery(handlers)["external_handler_calls_this_round"], 1)
        self.assertEqual(self.checks(handlers)["handler_calls"], 0)
        self.assertEqual(len(self.calls), 1); self.assertEqual(len(self.owners()), 1)
        state = self.memory.snapshot(); owner = next(iter(self.owners().values()))
        response = state["source_checks"][owner["active_response_id"]]
        self.assertTrue(response["complete"]); self.assertEqual(response["observed_at"], NOW)
        self.assertEqual(owner["binding"]["request"], prepare_meta_query("sink caddy"))
        self.assertEqual(len(state["concepts"]), 1)  # no fabricated direction concept
        task = next(row for row in state["query_tasks"].values() if row.get("kind") == "all_source_task_v1" and row["run_id"] == self.run)
        self.assertEqual(task["request"], {"query": "sink caddy", "country": "IN"})
        self.assertEqual(task["source_physical_request"], owner["binding"]["request"])
        self.assertEqual(task["generation"], 0)

    def test_check_first_then_direction_and_restart_cost_zero_extra_callback(self):
        handlers = self.handlers(); self.prepare(handlers)
        self.assertEqual(self.checks(handlers)["handler_calls"], 1)
        self.memory = ResearchMemory(self.memory.root, Journal(self.root / "finance"), evidence_type="synthetic")
        turn = open_work_turn(self.memory, "programme", at=self.now)
        with bind_work_turn(self.memory, turn["id"]):
            result = self.discovery(self.handlers())
        final = close_work_turn(self.memory, turn["id"], at=self.now, reason="Synthetic local reuse")
        self.assertEqual(result["external_handler_calls_this_round"], 0)
        self.assertEqual(final["counters"]["source_callbacks"], 0); self.assertEqual(len(self.calls), 1)

    def test_unknown_original_waits_in_both_lanes_without_second_browser(self):
        handlers = self.handlers(); self.prepare(handlers); self.mode = "unknown"
        self.checks(handlers); self.mode = "usable"
        self.checks(handlers); self.discovery(handlers)
        self.assertEqual(len(self.calls), 1); self.assertEqual(len(self.owners()), 1)
        self.assertEqual(next(iter(self.owners().values()))["state"], "dispatching")

    def test_legacy_unknown_is_adopted_without_inventing_zero_fee(self):
        handlers = self.handlers(False); self.prepare(handlers); self.mode = "unknown"
        execute_source_run(self.memory, self.run, handlers=handlers, as_of=self.now,
            bounded_call=lambda callback, context, timeout: callback(context))
        self.assertFalse(self.owners()); self.mode = "usable"
        self.discovery(self.handlers())
        self.assertEqual(len(self.calls), 1)
        owner = next(iter(self.owners().values()))
        self.assertEqual(owner["state"], "execution_unknown")
        self.assertEqual(owner["binding"]["incremental_collection_fee"], "unknown")

    def test_actual_empty_and_incomplete_empty_remain_distinct(self):
        handlers = self.handlers(); self.prepare(handlers); self.mode = "truncated"
        self.assertEqual(self.checks(handlers)["counts"], {"insufficient": 1})
        self.discovery(handlers); self.assertEqual(len(self.calls), 1)
        owner = next(iter(self.owners().values())); self.assertNotEqual(owner["state"], "completed_empty")

    def test_actual_empty_is_complete_without_a_product_or_market_vote(self):
        handlers = self.handlers(); self.prepare(handlers); self.mode = "empty"
        self.discovery(handlers)
        self.assertEqual(self.checks(handlers)["counts"], {"completed_no_results": 1})
        state = self.memory.snapshot(); self.assertEqual(state["observations"], {})
        self.assertEqual(state["market_reviews"], {}); self.assertEqual(next(iter(self.owners().values()))["state"], "completed_empty")

    def test_parser_failure_replays_owned_bytes_without_second_navigation(self):
        handlers = self.handlers(); self.prepare(handlers)
        with patch("product_radar.meta_browser.parse_meta_snapshot", side_effect=SafetyError("META_PUBLIC_SNAPSHOT_INVALID")):
            self.checks(handlers)
        self.discovery(handlers)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.memory.snapshot()["observations"]), 1)

    def test_scope_mismatch_is_not_host_refusal_or_absence(self):
        handlers = self.handlers(); self.prepare(handlers); self.mode = "scope_mismatch"
        self.assertEqual(self.checks(handlers)["counts"], {"insufficient": 1})
        self.assertIsNone(source_access_wait(self.memory.snapshot(), source_access_scope(prepare_meta_query("sink caddy")["url"]), as_of=self.now))
        self.discovery(handlers); self.assertEqual(len(self.calls), 1)

    def test_actual_429_waits_across_terms_and_honors_original_retry_after(self):
        handlers = self.handlers(); self.prepare(handlers); self.mode = "blocked"
        self.checks(handlers)
        wait = source_access_wait(self.memory.snapshot(), source_access_scope(prepare_meta_query("sink caddy")["url"]), as_of=self.now)
        self.assertEqual(wait["next_retry_at"], "2026-10-09T11:00:00+00:00")
        self.prepare(handlers, query="garbage bags", run="second-checks")
        self.checks(handlers); self.assertEqual(len(self.calls), 1)

    def test_due_new_caller_joins_one_actual_host_probe_then_healthy_terms_continue(self):
        handlers = self.handlers(); self.prepare(handlers); self.mode = "blocked"
        self.checks(handlers); original = next(iter(self.owners()))
        self.now = "2026-10-09T11:00:00+00:00"; self.mode = "usable"
        self.prepare(handlers, run="new-caller")
        self.assertEqual(self.checks(handlers)["counts"], {"acquired": 1})
        self.assertEqual(len(self.calls), 2)
        latest = max(self.owners().values(), key=lambda row: row["binding"]["generation"])
        self.assertEqual(latest["binding"]["previous_acquisition_id"], original)
        self.prepare(handlers, query="garbage bags", run="healthy-after-recovery")
        self.checks(handlers); self.assertEqual(len(self.calls), 3)

    def test_default_is_legacy_and_neutral_boolean_cannot_launch(self):
        handlers = self.handlers(False); self.prepare(handlers)
        execute_source_run(self.memory, self.run, handlers=handlers, as_of=self.now,
            bounded_call=lambda callback, context, timeout: callback(context))
        self.assertEqual(self.owners(), {})
        with self.assertRaisesRegex(SafetyError, "SOURCE_SHARED_ADMISSION_REQUIRED"):
            self.handlers()["meta-ads"]["handler"]({"route": "meta-ads", "request": {"query": "sink caddy"},
                "acquisition_id": "unadmitted", "as_of": self.now})
        self.assertEqual(len(self.calls), 1)

    def test_legacy_saved_original_keeps_id_date_without_a_second_read(self):
        handlers = self.handlers(False); self.prepare(handlers)
        execute_source_run(self.memory, self.run, handlers=handlers, as_of=self.now,
            bounded_call=lambda callback, context, timeout: callback(context))
        original = next(iter(self.memory.snapshot()["captures"].values()))["acquisition_id"]
        self.discovery(self.handlers())
        self.assertEqual(list(self.owners()), [original]); self.assertEqual(len(self.calls), 1)
        self.assertEqual({r["observed_at"] for r in self.memory.snapshot()["captures"].values()}, {NOW})

    def test_changed_saved_bytes_do_not_authorise_collection(self):
        from product_radar.storage import atomic_json, load_json
        handlers = self.handlers(); self.prepare(handlers); self.checks(handlers)
        folder = Path(self.calls[0][1]); snapshot = load_json(folder / "snapshot.json")
        snapshot["text"] = "Changed original"
        atomic_json(folder / "snapshot.json", snapshot)
        self.discovery(handlers)
        self.assertEqual(len(self.calls), 1)

    def test_failed_global_intent_save_is_provably_unstarted_and_can_resume(self):
        handlers = self.handlers(); self.prepare(handlers)
        with patch("product_radar.source_acquisitions.mark_source_dispatching", side_effect=OSError("Synthetic local write failure")):
            self.checks(handlers)
        self.assertFalse(self.calls); original_id = next(iter(self.owners()))
        self.discovery(handlers)
        self.assertEqual(len(self.calls), 1); self.assertEqual(list(self.owners()), [original_id])

    def test_expired_complete_original_gets_one_linked_refresh_not_redated_bytes(self):
        from product_radar.catalogue_discovery import advance_discovery_waves
        handlers = self.handlers(); self.prepare(handlers); self.mode = "empty"; self.discovery(handlers)
        original_id = next(iter(self.owners())); self.now = "2026-10-17T10:00:00+00:00"
        turn = open_work_turn(self.memory, "programme", at=self.now)
        with bind_work_turn(self.memory, turn["id"]):
            advance_discovery_waves(self.memory, "programme", fixture(), handlers=handlers,
                as_of=self.now, admission=lambda *_args, **_options: None)
        close_work_turn(self.memory, turn["id"], at=self.now, reason="Synthetic due refresh")
        self.assertEqual(len(self.calls), 2); self.assertEqual(len(self.owners()), 2)
        latest = max(self.owners().values(), key=lambda row: row["binding"]["generation"])
        self.assertEqual(latest["binding"]["previous_acquisition_id"], original_id)
        self.assertEqual({r["observed_at"] for r in self.memory.snapshot()["captures"].values()}, {NOW, self.now})


if __name__ == "__main__":
    unittest.main()
