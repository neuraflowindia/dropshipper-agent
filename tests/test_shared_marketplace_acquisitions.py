"""Connected original-owner proofs using only saved synthetic browser bodies."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar.catalogue_discovery import advance_discovery_cycle, prepare_discovery_cycle
from product_radar.marketplace_browser import build_marketplace_request, capture_marketplace
from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory, key
from product_radar.source_acquisitions import ACQUISITION_KIND
from product_radar.source_coverage import create_marketplace_handlers, execute_source_run, prepare_source_tasks
from product_radar.storage import Journal
from product_radar.storage import atomic_json
from product_radar.work_turns import bind_work_turn, open_work_turn
from test_catalogue_expansion import fixture
from test_marketplace_browser import snapshot, transport


NOW = "2026-10-09T10:00:00+00:00"


class SharedMarketplaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.calls = []
        self.source_root = self.memory.root / "direct_marketplace"
        self.memory.put("concepts", "sink", {"status": "active", "description": {"label": "Sink caddy",
            "category": "Home and Kitchen", "function": "Store sink essentials", "mechanism": "manual",
            "audience": "adult", "features": ["draining"], "brand": "generic_or_small",
            "basis": "observed", "reason": "Synthetic source fixture."}})

    def handlers(self, *, changes=None, failure=None, shared=True):
        def capture(request, acquisition_id, **options):
            self.calls.append((acquisition_id, deepcopy(request)))
            if failure: raise failure
            with patch("product_radar.marketplace_browser.utc_now", return_value=NOW):
                return capture_marketplace(request, acquisition_id, **options,
                    snapshot_reader=lambda req: transport(snapshot(req, **(changes or {})), navigation_started_at=NOW))
        return create_marketplace_handlers(self.memory, data_root=self.source_root,
                                           capture=capture, shared_acquisitions=shared)

    def prepare(self, handlers, *, request=None, programme="discovery-one", run="checks-one"):
        request = request or {"query": "sink caddy", "country": "IN", "limit": 30}
        self.run_id = run
        prepare_source_tasks(self.memory, [{"route": "amazon-search", "request": request,
            "concept_ids": ["sink"]}], run_id=run, as_of=NOW)
        self.cycle = prepare_discovery_cycle(self.memory, programme, fixture(),
            handlers={"amazon-search": handlers["amazon-search"]}, include_lists=False, action_limit=1, as_of=NOW)

    def checks(self, handlers, **options):
        return execute_source_run(self.memory, self.run_id, handlers=handlers, as_of=NOW,
            bounded_call=lambda callback, context, timeout: callback(context), shared_marketplace=True, **options)

    def discovery(self, handlers):
        return advance_discovery_cycle(self.memory, self.cycle["id"],
            handlers={"amazon-search": handlers["amazon-search"]}, as_of=NOW,
            admission=lambda *_args, **_kwargs: None, shared_marketplace=True)

    def owners(self):
        return {rid: row for rid, row in self.memory.snapshot()["batch_runs"].items()
                if row.get("kind") == ACQUISITION_KIND}

    def test_delayed_completion_uses_actual_time_across_iso_offsets(self):
        started = "2026-10-09T15:30:00+05:30"
        completed, committed = "2026-10-09T10:00:03+00:00", "2026-10-09T10:00:05+00:00"
        def capture(request, acquisition_id, **options):
            self.calls.append((acquisition_id, deepcopy(request)))
            with patch("product_radar.marketplace_browser.utc_now", return_value=completed):
                return capture_marketplace(request, acquisition_id, **options,
                    snapshot_reader=lambda req: transport(snapshot(req), navigation_started_at=started,
                                                          completed_at=completed))
        handlers = create_marketplace_handlers(self.memory, data_root=self.source_root,
            capture=capture, shared_acquisitions=True)
        self.prepare(handlers)
        with patch("product_radar.source_coverage.utc_now", return_value=committed):
            result = execute_source_run(self.memory, self.run_id, handlers=handlers, as_of=started,
                shared_marketplace=True, bounded_call=lambda callback, context, timeout: callback(context))
        self.assertEqual(result["counts"], {"acquired": 1})
        state = self.memory.snapshot(); owner = next(iter(self.owners().values()))
        response = state["source_checks"][owner["active_response_id"]]
        self.assertEqual(response["observed_at"], completed)
        self.assertEqual(response["recorded_at"], committed)
        self.assertEqual(len(self.calls), 1)

    def test_discovery_first_then_checks_share_exact_original_without_callback(self):
        handlers = self.handlers(); self.prepare(handlers)
        first = self.discovery(handlers)
        second = self.checks(handlers)
        self.assertEqual(first["external_handler_calls_this_round"], 1)
        self.assertEqual(second["handler_calls"], 0)
        self.assertEqual(second["counts"], {"acquired": 1})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.owners()), 1)
        captures = list(self.memory.snapshot()["captures"].values())
        self.assertEqual({r["acquisition_id"] for r in captures}, {self.calls[0][0]})
        self.assertEqual({r["observed_at"] for r in captures}, {NOW})
        task = next(iter(self.memory.snapshot()["query_tasks"].values()))
        self.assertEqual(task["request"], {"query": "sink caddy", "country": "IN", "limit": 30})
        self.assertEqual(task["marketplace_physical_request"], self.calls[0][1])
        self.assertEqual(task["generation"], 0)

    def test_checks_first_then_discovery_reuses_without_new_dispatch(self):
        handlers = self.handlers(); self.prepare(handlers)
        self.assertEqual(self.checks(handlers)["handler_calls"], 1)
        second = self.discovery(handlers)
        self.assertEqual(second["external_handler_calls_this_round"], 0)
        self.assertEqual(second["actual_dispatches"], 0)
        self.assertEqual(second["captured_cards"], 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.owners()), 1)

    def test_restart_replays_one_original_owner_and_does_not_recollect(self):
        handlers = self.handlers(); self.prepare(handlers); self.discovery(handlers)
        self.memory = ResearchMemory(self.memory.root, Journal(self.root / "finance"), evidence_type="synthetic")
        self.assertEqual(self.checks(self.handlers())["handler_calls"], 0)
        self.assertEqual(len(self.calls), 1)

    def test_unknown_new_origin_and_other_consumer_wait_without_collection_fallback(self):
        handlers = self.handlers(failure=TimeoutError("Synthetic original interrupted")); self.prepare(handlers)
        self.checks(handlers)
        self.assertEqual(next(iter(self.owners().values()))["state"], "dispatching")
        # Both original recovery and a different consumer stay local-only.
        self.checks(self.handlers()); self.discovery(self.handlers())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.owners()), 1)
        self.assertEqual(list(self.memory.snapshot()["captures"]), [])

    def test_legacy_uncertain_intent_is_adopted_as_unknown_not_unstarted(self):
        old = self.handlers(failure=TimeoutError("Synthetic legacy interrupted"), shared=False)
        self.prepare(old)
        execute_source_run(self.memory, self.run_id, handlers=old, as_of=NOW,
            bounded_call=lambda callback, context, timeout: callback(context))
        original = next(iter(self.memory.snapshot()["query_tasks"].values()))["acquisition_id"]
        self.assertEqual(self.owners(), {})
        self.discovery(self.handlers())
        self.assertEqual(list(self.owners()), [original])
        self.assertEqual(self.owners()[original]["state"], "execution_unknown")
        self.assertEqual(len(self.calls), 1)

    def test_legacy_verified_capture_is_adopted_preserving_actual_id_and_date(self):
        old = self.handlers(shared=False); self.prepare(old)
        execute_source_run(self.memory, self.run_id, handlers=old, as_of=NOW,
            bounded_call=lambda callback, context, timeout: callback(context))
        original = self.calls[0][0]
        self.assertEqual(self.owners(), {})
        result = self.discovery(self.handlers())
        self.assertEqual(result["external_handler_calls_this_round"], 0)
        self.assertEqual(list(self.owners()), [original])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual({r["observed_at"] for r in self.memory.snapshot()["captures"].values()}, {NOW})

    def test_recognized_empty_is_completed_capture_not_positive_market_evidence(self):
        handlers = self.handlers(changes={"cards": [], "card_count": 0, "empty": True}); self.prepare(handlers)
        self.discovery(handlers)
        result = self.checks(handlers)
        self.assertEqual(result["counts"], {"completed_no_results": 1})
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(next(iter(self.owners().values()))["state"], "completed_empty")
        self.assertEqual(self.memory.snapshot()["observations"], {})

    def test_truncated_empty_does_not_claim_completed_absence(self):
        handlers = self.handlers(changes={"cards": [], "card_count": 0, "empty": True, "text_truncated": True})
        self.prepare(handlers)
        result = self.checks(handlers)
        self.assertEqual(result["counts"], {"insufficient": 1})
        self.discovery(handlers)
        self.assertEqual(len(self.calls), 1)
        owner = next(iter(self.owners().values()))
        self.assertNotEqual(owner["state"], "completed_empty")
        self.assertTrue(self.memory.snapshot()["source_checks"][owner["active_response_id"]]["coverage"]["byte_truncated"])

    def test_scope_mismatch_is_not_empty_and_not_shared_positive_evidence(self):
        handlers = self.handlers(changes={"query": "garbage bags"}); self.prepare(handlers)
        first = self.checks(handlers)
        self.assertEqual(first["counts"], {"insufficient": 1})
        second = self.discovery(handlers)
        self.assertEqual(second["captured_cards"], 0)
        self.assertEqual(second["external_handler_calls_this_round"], 0)
        owner = next(iter(self.owners().values()))
        stage = self.memory.snapshot()["source_checks"][owner["active_response_id"]]
        self.assertFalse(stage["complete"])
        self.assertFalse(stage["coverage"]["recognized"])
        self.assertEqual(len(self.calls), 1)

    def test_parser_local_failure_retains_original_response_never_navigation_retry(self):
        handlers = self.handlers(changes={"cards": "invalid saved source snapshot"}); self.prepare(handlers)
        self.checks(handlers)
        second = self.discovery(handlers)
        self.assertEqual(second["external_handler_calls_this_round"], 0)
        self.assertEqual(len(self.calls), 1)
        owner = next(iter(self.owners().values()))
        stage = self.memory.snapshot()["source_checks"][owner["active_response_id"]]
        self.assertEqual(stage["source_state"], "failed")
        self.assertIn("snapshot.json", stage["artifacts"])
        self.assertFalse(stage["complete"])
        self.assertEqual(self.memory.snapshot()["observations"], {})

    def test_complete_request_bounds_do_not_share_different_limits(self):
        handlers = self.handlers(); self.prepare(handlers, request=build_marketplace_request("amazon-search", term="sink caddy", limit=10))
        self.checks(handlers); self.discovery(handlers)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.owners()), 2)
        self.assertEqual({request["limit"] for _, request in self.calls}, {10, 30})

    def test_saved_reuse_spends_zero_shared_turn_callbacks(self):
        handlers = self.handlers(); self.prepare(handlers)
        turn = open_work_turn(self.memory, "programme", at=NOW)
        with bind_work_turn(self.memory, turn["id"]):
            self.discovery(handlers)
            before = self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["source_callbacks"]
            self.checks(handlers)
            after = self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["source_callbacks"]
        self.assertEqual(before, 1)
        self.assertEqual(after, before)

    def test_explicit_shared_factory_refuses_direct_unadmitted_capture(self):
        handlers = self.handlers()
        with self.assertRaisesRegex(SafetyError, "SOURCE_MARKETPLACE_ADMISSION_REQUIRED"):
            handlers["amazon-search"]["handler"]({"route": "amazon-search", "acquisition_id": "unadmitted",
                "request": {"query": "sink caddy"}, "as_of": NOW, "concept_ids": [], "description_hashes": {}})
        self.assertEqual(self.calls, [])

    def test_defaults_remain_legacy_and_create_no_owner(self):
        handlers = self.handlers(shared=False); self.prepare(handlers)
        result = execute_source_run(self.memory, self.run_id, handlers=handlers, as_of=NOW,
            bounded_call=lambda callback, context, timeout: callback(context))
        self.assertEqual(result["handler_calls"], 1)
        self.assertEqual(self.owners(), {})

    def test_context_boolean_alone_is_not_caller_intent_or_launch_authority(self):
        handlers = self.handlers(); self.prepare(handlers)
        question = self.cycle["planned_actions"][0]["questions"][0]
        context = {"task_id": question["id"], "route": "amazon-search", "acquisition_id": "unused-original",
                   "request": question["request"], "as_of": NOW, "concept_ids": [], "description_hashes": {}}
        resolved = handlers["amazon-search"]["resolve_marketplace"](context)
        with self.assertRaisesRegex(SafetyError, "SOURCE_MARKETPLACE_CALLER_INTENT_REQUIRED"):
            handlers["amazon-search"]["handler"](dict(context, acquisition_id=resolved["acquisition_id"],
                marketplace_admitted=True, marketplace_caller={"table": "batch_runs", "id": self.cycle["id"]}))
        self.assertEqual(self.calls, [])
        self.assertEqual(next(iter(self.owners().values()))["state"], "reserved")

    def test_legacy_intent_does_not_invent_a_free_origin_without_original_adapter_proof(self):
        handlers = self.handlers(failure=TimeoutError("Synthetic legacy original"), shared=False)
        self.prepare(handlers)
        execute_source_run(self.memory, self.run_id, handlers=handlers, as_of=NOW,
            bounded_call=lambda callback, context, timeout: callback(context))
        self.discovery(self.handlers())
        binding = next(iter(self.owners().values()))["binding"]
        self.assertEqual(binding["collection_channel"], "unknown")
        self.assertEqual(binding["incremental_collection_fee"], "unknown")

    def test_original_file_hint_changes_without_adoption_import_or_callback(self):
        handlers = self.handlers(failure=TimeoutError("Synthetic original interrupted")); self.prepare(handlers)
        self.checks(handlers)
        task_id, task = next(iter(self.memory.snapshot()["query_tasks"].items()))
        context = {"task_id": task_id, "route": task["route"], "request": task["request"]}
        helper = handlers["amazon-search"]["marketplace_saved_hint"]
        before_state = self.memory.snapshot()
        before = helper(context)
        aid = before["acquisition_id"]
        atomic_json(self.source_root / aid / "native_result.json", {"synthetic": "Late original saved file"})
        after = helper(context)
        self.assertNotEqual(after, before)
        self.assertEqual(self.memory.snapshot(), before_state)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(after["files"][0][0], "native_result.json")

    def test_original_artifacts_changed_after_capture_are_not_reused_or_recollected(self):
        handlers = self.handlers(); self.prepare(handlers); self.discovery(handlers)
        aid = self.calls[0][0]
        atomic_json(self.source_root / aid / "snapshot.json", {"changed": "Synthetic damage"})
        result = self.checks(handlers)
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(result["counts"], {"blocked": 1})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.owners()), 1)

    def test_actual_legacy_artifact_area_can_be_explicitly_bound_without_copying(self):
        original_root = self.root / "retained_originals"
        def original_capture(request, acquisition_id, **options):
            self.calls.append((acquisition_id, deepcopy(request)))
            with patch("product_radar.marketplace_browser.utc_now", return_value=NOW):
                return capture_marketplace(request, acquisition_id, **options,
                    snapshot_reader=lambda req: transport(snapshot(req), navigation_started_at=NOW))
        legacy = create_marketplace_handlers(self.memory, data_root=original_root, capture=original_capture)
        self.prepare(legacy)
        execute_source_run(self.memory, self.run_id, handlers=legacy, as_of=NOW,
            bounded_call=lambda callback, context, timeout: callback(context))
        aid = self.calls[0][0]
        original = (original_root / aid / "receipt.json").read_bytes()
        shared = create_marketplace_handlers(self.memory, data_root=self.source_root,
            capture=lambda *_args, **_kwargs: self.fail("Must only replay the actual original"),
            shared_acquisitions=True, artifact_roots=[original_root])
        result = self.discovery(shared)
        self.assertEqual(result["external_handler_calls_this_round"], 0)
        self.assertEqual(list(self.owners()), [aid])
        self.assertFalse((self.source_root / aid).exists())
        self.assertEqual((original_root / aid / "receipt.json").read_bytes(), original)

    def test_future_retry_does_not_authorize_original_callback_but_saved_result_applies(self):
        handlers = self.handlers(); self.prepare(handlers)
        with patch("product_radar.source_acquisitions.mark_source_dispatching", side_effect=OSError("Synthetic local save conflict")):
            self.checks(handlers)
        tid, old = next(iter(self.memory.snapshot()["query_tasks"].items()))
        self.memory.put("query_tasks", tid, dict(old, next_retry_at="2026-10-10T10:00:00+00:00"), previous=old)
        self.assertEqual(self.checks(handlers)["counts"], {"waiting": 1})
        self.assertEqual(self.calls, [])
        # Another healthy caller can complete the same never-entered original.
        self.discovery(handlers)
        result = self.checks(handlers)
        self.assertEqual(result["counts"], {"acquired": 1})
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
