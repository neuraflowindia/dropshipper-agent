"""Connected original-owner proofs using only saved synthetic browser bodies."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar.marketplace_browser import build_marketplace_request, capture_marketplace
from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory, key
from product_radar.source_acquisitions import ACQUISITION_KIND
from product_radar.source_coverage import create_marketplace_handlers, execute_source_run, prepare_source_tasks
from product_radar.storage import Journal
from product_radar.storage import atomic_json
from product_radar.work_turns import DEFAULT_POLICY, bind_work_turn, open_work_turn
from test_marketplace_browser import snapshot, transport


NOW = "2026-10-09T10:00:00+00:00"


def wall_clock(instant):
    """Real utc_now shape at one instant: whole seconds unless asked for finer."""
    moment = datetime.fromisoformat(instant)
    return lambda timespec="seconds": moment.astimezone(timezone.utc).isoformat(timespec=timespec)


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
        self.memory.put("concepts", "rack", {"status": "active", "description": {"label": "Dish rack",
            "category": "Home and Kitchen", "function": "Dry washed dishes", "mechanism": "manual",
            "audience": "adult", "features": ["draining"], "brand": "generic_or_small",
            "basis": "observed", "reason": "Synthetic second type asking the same search."}})

    def handlers(self, *, changes=None, failure=None, shared=True):
        def capture(request, acquisition_id, **options):
            self.calls.append((acquisition_id, deepcopy(request)))
            if failure: raise failure
            with patch("product_radar.marketplace_browser.utc_now", return_value=NOW):
                return capture_marketplace(request, acquisition_id, **options,
                    snapshot_reader=lambda req: transport(snapshot(req, **(changes or {})), navigation_started_at=NOW))
        return create_marketplace_handlers(self.memory, data_root=self.source_root,
                                           capture=capture, shared_acquisitions=shared)

    def prepare(self, handlers, *, request=None, run="checks-one"):
        """Two consumers of one Amazon read: this run's crowding question and
        another programme's same question (Amazon is never discovery, D4)."""
        request = request or {"query": "sink caddy", "country": "IN", "limit": 30}
        self.run_id = run
        prepare_source_tasks(self.memory, [{"route": "amazon-search", "request": request,
            "concept_ids": ["sink"]}], run_id=run, as_of=NOW)
        prepare_source_tasks(self.memory, [{"route": "amazon-search",
            "request": {"query": "sink caddy", "country": "IN", "limit": 30}, "concept_ids": ["rack"]}],
            run_id="checks-other", as_of=NOW)

    def checks(self, handlers, **options):
        return execute_source_run(self.memory, self.run_id, handlers=handlers, as_of=NOW,
            bounded_call=lambda callback, context, timeout: callback(context), shared_marketplace=True, **options)

    def other(self, handlers):
        return execute_source_run(self.memory, "checks-other", handlers=handlers, as_of=NOW,
            bounded_call=lambda callback, context, timeout: callback(context), shared_marketplace=True)

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

    def run_native_subsecond(self, completed, commit_instant):
        # Owned Windows Chrome stamps 7-digit "Z" completion times while the
        # wrapper's own clock is whole-second; the commit happens just after.
        def capture(request, acquisition_id, **options):
            self.calls.append((acquisition_id, deepcopy(request)))
            with patch("product_radar.marketplace_browser.utc_now", return_value="2026-10-09T10:00:05+00:00"):
                return capture_marketplace(request, acquisition_id, **options,
                    snapshot_reader=lambda req: transport(snapshot(req), navigation_started_at="2026-10-09T10:00:01.7059311Z",
                                                          completed_at=completed))
        handlers = create_marketplace_handlers(self.memory, data_root=self.source_root,
            capture=capture, shared_acquisitions=True)
        self.prepare(handlers)
        with patch("product_radar.source_coverage.utc_now", wall_clock(commit_instant)):
            return execute_source_run(self.memory, self.run_id, handlers=handlers, as_of=NOW,
                shared_marketplace=True, bounded_call=lambda callback, context, timeout: callback(context))

    def test_native_completion_earlier_in_the_commit_second_is_saved(self):
        # 13 Oct first round: observed 19:48:56.2587705Z, committed at 19:48:56.4
        # but stamped "19:48:56" -> SOURCE_ACQUISITION_SOURCE_DATE_INVALID.
        completed = "2026-10-09T10:00:05.2587705Z"
        result = self.run_native_subsecond(completed, "2026-10-09T10:00:05.420000+00:00")
        self.assertEqual(result["counts"], {"acquired": 1})
        owner = next(iter(self.owners().values()))
        self.assertEqual(owner["state"], "response_saved")
        response = self.memory.snapshot()["source_checks"][owner["active_response_id"]]
        self.assertEqual(response["observed_at"], completed)
        self.assertEqual(response["recorded_at"], "2026-10-09T10:00:05.420000+00:00")
        self.assertEqual(len(self.calls), 1)

    def test_receipt_dated_after_the_actual_commit_instant_is_still_refused(self):
        result = self.run_native_subsecond("2026-10-09T10:00:05.9000000Z", "2026-10-09T10:00:05.420000+00:00")
        self.assertEqual(result["counts"], {"blocked": 1})
        task = next(row for row in self.memory.snapshot()["query_tasks"].values()
                    if row.get("kind") == "all_source_task_v1" and row["run_id"] == self.run_id)
        self.assertEqual(task["reason"], "SOURCE_ACQUISITION_SOURCE_DATE_INVALID")
        owner = next(iter(self.owners().values()))
        self.assertEqual(owner["state"], "dispatching"); self.assertIsNone(owner["active_response_id"])
        self.assertEqual(len(self.calls), 1)

    def test_other_consumer_first_then_checks_share_exact_original_without_callback(self):
        handlers = self.handlers(); self.prepare(handlers)
        first = self.other(handlers)
        second = self.checks(handlers)
        self.assertEqual(first["handler_calls"], 1)
        self.assertEqual(second["handler_calls"], 0)
        self.assertEqual(second["counts"], {"acquired": 1})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.owners()), 1)
        # The reused reply is named as a saved answer, never a fresh dispatch.
        self.assertEqual(first["saved_answer_refs"], [])
        self.assertEqual(second["saved_answer_refs"], [self.calls[0][0]])
        captures = list(self.memory.snapshot()["captures"].values())
        self.assertEqual({r["acquisition_id"] for r in captures}, {self.calls[0][0]})
        self.assertEqual({r["observed_at"] for r in captures}, {NOW})
        task = next(row for row in self.memory.snapshot()["query_tasks"].values() if row["run_id"] == self.run_id)
        self.assertEqual(task["request"], {"query": "sink caddy", "country": "IN", "limit": 30})
        self.assertEqual(task["marketplace_physical_request"], self.calls[0][1])
        self.assertEqual(task["generation"], 0)

    def test_checks_first_then_other_consumer_reuses_without_new_dispatch(self):
        handlers = self.handlers(); self.prepare(handlers)
        self.assertEqual(self.checks(handlers)["handler_calls"], 1)
        second = self.other(handlers)
        self.assertEqual(second["handler_calls"], 0)
        self.assertEqual(second["counts"], {"acquired": 1})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.owners()), 1)

    def test_restart_replays_one_original_owner_and_does_not_recollect(self):
        handlers = self.handlers(); self.prepare(handlers); self.other(handlers)
        self.memory = ResearchMemory(self.memory.root, Journal(self.root / "finance"), evidence_type="synthetic")
        self.assertEqual(self.checks(self.handlers())["handler_calls"], 0)
        self.assertEqual(len(self.calls), 1)

    def test_unknown_new_origin_and_other_consumer_wait_without_collection_fallback(self):
        handlers = self.handlers(failure=TimeoutError("Synthetic original interrupted")); self.prepare(handlers)
        self.checks(handlers)
        self.assertEqual(next(iter(self.owners().values()))["state"], "dispatching")
        # Both original recovery and a different consumer stay local-only.
        self.checks(self.handlers()); self.other(self.handlers())
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
        self.other(self.handlers())
        self.assertEqual(list(self.owners()), [original])
        self.assertEqual(self.owners()[original]["state"], "execution_unknown")
        self.assertEqual(len(self.calls), 1)

    def test_legacy_verified_capture_is_adopted_preserving_actual_id_and_date(self):
        old = self.handlers(shared=False); self.prepare(old)
        execute_source_run(self.memory, self.run_id, handlers=old, as_of=NOW,
            bounded_call=lambda callback, context, timeout: callback(context))
        original = self.calls[0][0]
        self.assertEqual(self.owners(), {})
        result = self.other(self.handlers())
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(list(self.owners()), [original])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual({r["observed_at"] for r in self.memory.snapshot()["captures"].values()}, {NOW})

    def test_recognized_empty_is_completed_capture_not_positive_market_evidence(self):
        handlers = self.handlers(changes={"cards": [], "card_count": 0, "empty": True}); self.prepare(handlers)
        self.other(handlers)
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
        self.other(handlers)
        self.assertEqual(len(self.calls), 1)
        owner = next(iter(self.owners().values()))
        self.assertNotEqual(owner["state"], "completed_empty")
        self.assertTrue(self.memory.snapshot()["source_checks"][owner["active_response_id"]]["coverage"]["byte_truncated"])

    def test_scope_mismatch_is_not_empty_and_not_shared_positive_evidence(self):
        handlers = self.handlers(changes={"query": "garbage bags"}); self.prepare(handlers)
        first = self.checks(handlers)
        self.assertEqual(first["counts"], {"insufficient": 1})
        second = self.other(handlers)
        self.assertEqual(second["counts"], {"insufficient": 1})
        self.assertEqual(second["handler_calls"], 0)
        owner = next(iter(self.owners().values()))
        stage = self.memory.snapshot()["source_checks"][owner["active_response_id"]]
        self.assertFalse(stage["complete"])
        self.assertFalse(stage["coverage"]["recognized"])
        self.assertEqual(len(self.calls), 1)

    def test_parser_local_failure_retains_original_response_never_navigation_retry(self):
        handlers = self.handlers(changes={"cards": "invalid saved source snapshot"}); self.prepare(handlers)
        self.checks(handlers)
        second = self.other(handlers)
        self.assertEqual(second["handler_calls"], 0)
        self.assertEqual(len(self.calls), 1)
        owner = next(iter(self.owners().values()))
        stage = self.memory.snapshot()["source_checks"][owner["active_response_id"]]
        self.assertEqual(stage["source_state"], "failed")
        self.assertIn("snapshot.json", stage["artifacts"])
        self.assertFalse(stage["complete"])
        self.assertEqual(self.memory.snapshot()["observations"], {})

    def test_complete_request_bounds_do_not_share_different_limits(self):
        handlers = self.handlers(); self.prepare(handlers, request=build_marketplace_request("amazon-search", term="sink caddy", limit=10))
        self.checks(handlers); self.other(handlers)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.owners()), 2)
        self.assertEqual({request["limit"] for _, request in self.calls}, {10, 30})

    def test_saved_reuse_spends_zero_shared_turn_callbacks(self):
        handlers = self.handlers(); self.prepare(handlers)
        turn = open_work_turn(self.memory, "programme", at=NOW)
        with bind_work_turn(self.memory, turn["id"]):
            self.other(handlers)
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
        task_id, question = next((tid, row) for tid, row in self.memory.snapshot()["query_tasks"].items()
                                 if row["run_id"] == "checks-other")
        context = {"task_id": task_id, "route": "amazon-search", "acquisition_id": "unused-original",
                   "request": question["request"], "as_of": NOW, "concept_ids": [], "description_hashes": {}}
        resolved = handlers["amazon-search"]["resolve_marketplace"](context)
        with self.assertRaisesRegex(SafetyError, "SOURCE_MARKETPLACE_CALLER_INTENT_REQUIRED"):
            handlers["amazon-search"]["handler"](dict(context, acquisition_id=resolved["acquisition_id"],
                marketplace_admitted=True, marketplace_caller={"table": "query_tasks", "id": task_id}))
        self.assertEqual(self.calls, [])
        self.assertEqual(next(iter(self.owners().values()))["state"], "reserved")

    def test_legacy_intent_does_not_invent_a_free_origin_without_original_adapter_proof(self):
        handlers = self.handlers(failure=TimeoutError("Synthetic legacy original"), shared=False)
        self.prepare(handlers)
        execute_source_run(self.memory, self.run_id, handlers=handlers, as_of=NOW,
            bounded_call=lambda callback, context, timeout: callback(context))
        self.other(self.handlers())
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
        handlers = self.handlers(); self.prepare(handlers); self.other(handlers)
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
        result = self.other(shared)
        self.assertEqual(result["handler_calls"], 0)
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
        self.other(handlers)
        result = self.checks(handlers)
        self.assertEqual(result["counts"], {"acquired": 1})
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(len(self.calls), 1)

    # 16 Oct power cut: recorded reply files were left empty. The lost read is
    # set aside once; its question reads afresh under the normal limits.

    def lose(self, path):
        with open(path, "wb"):
            pass  # Empty, as a write cut short by the power cut left it.

    def task(self, run_id):
        return next(row for row in self.memory.snapshot()["query_tasks"].values()
                    if row.get("kind") == "all_source_task_v1" and row["run_id"] == run_id)

    def test_lost_saved_bytes_are_set_aside_and_read_once_more_within_normal_limits(self):
        handlers = self.handlers(); self.prepare(handlers)
        policy = dict(deepcopy(DEFAULT_POLICY), source_callbacks=1, exploration_slots=0)
        turn = open_work_turn(self.memory, "programme", at=NOW, policy=policy)
        with bind_work_turn(self.memory, turn["id"]):
            self.assertEqual(self.other(handlers)["handler_calls"], 1)
            lost = self.calls[0][0]; folder = self.source_root / lost
            kept = {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()}
            self.lose(folder / "snapshot.json")
            # The turn's only source callback is spent: the lost read is set
            # aside, and its replacement waits for the next allowance.
            held = self.checks(handlers)
        self.assertEqual((held["handler_calls"], held["counts"]), (0, {"not_attempted": 1}))
        owners = self.owners()
        self.assertEqual((owners[lost]["state"], owners[lost]["active_response_id"]), ("set_aside", None))
        (replacement,) = [aid for aid in owners if aid != lost]
        self.assertEqual((owners[replacement]["state"], owners[replacement]["binding"]["damage_recovery"]["acquisition_id"]),
                         ("reserved", lost))
        self.assertEqual(self.task(self.run_id)["tries"], 0)
        # The next allowance: exactly one fresh read, counted against the question.
        result = self.checks(handlers)
        self.assertEqual((result["handler_calls"], result["counts"]), (1, {"acquired": 1}))
        self.assertEqual([call[0] for call in self.calls], [lost, replacement])
        task = self.task(self.run_id)
        self.assertEqual((task["acquisition_id"], task["attempt_refs"], task["tries"], task["generation"]),
                         (replacement, [replacement], 1, 1))
        receipt = self.memory.snapshot()["source_checks"][task["receipt_refs"][-1]]
        self.assertEqual(receipt["envelope"]["acquisition_id"], replacement)
        # Nothing of the lost read was deleted or rewritten.
        self.assertEqual({path.name for path in folder.iterdir() if path.is_file()}, set(kept))
        self.assertEqual({name: (folder / name).read_bytes() for name in kept if name != "snapshot.json"},
                         {name: raw for name, raw in kept.items() if name != "snapshot.json"})
        # Later passes and new consumers use the replacement; nothing reads again.
        self.assertEqual(self.checks(handlers)["handler_calls"], 0)
        prepare_source_tasks(self.memory, [{"route": "amazon-search", "request": {"query": "sink caddy", "country": "IN",
            "limit": 30}, "concept_ids": ["rack"]}], run_id="checks-third", as_of=NOW)
        third = execute_source_run(self.memory, "checks-third", handlers=handlers, as_of=NOW,
            bounded_call=lambda callback, context, timeout: callback(context), shared_marketplace=True)
        self.assertEqual((third["handler_calls"], third["counts"], third["saved_answer_refs"]), (0, {"acquired": 1}, [replacement]))
        self.assertEqual((len(self.calls), len(self.owners())), (2, 2))

    def borrowed(self, handlers):
        """A follow-on question that borrowed the other run's saved read, then the loss."""
        self.other(handlers)
        prepare_source_tasks(self.memory, [{"route": "amazon-search", "request": {"query": "sink caddy", "country": "IN",
            "limit": 30}, "concept_ids": ["sink"]}], run_id="follow-on", as_of=NOW, reuse_run_ids=["checks-other"])
        task = self.task("follow-on")
        lost = self.calls[0][0]
        self.assertEqual((task["status"], task["acquisition_id"], task["reconcile_required"], task["attempt_refs"]),
                         ("dispatching", lost, True, []))
        self.assertTrue(task["reused_acquisition_ref"])
        self.lose(self.source_root / lost / "snapshot.json")
        return lost

    def run_follow_on(self, handlers):
        return execute_source_run(self.memory, "follow-on", handlers=handlers, as_of=NOW,
            bounded_call=lambda callback, context, timeout: callback(context), shared_marketplace=True)

    def test_a_borrowed_lost_read_becomes_the_questions_own_counted_read(self):
        handlers = self.handlers(); self.prepare(handlers); lost = self.borrowed(handlers)
        result = self.run_follow_on(handlers)
        self.assertEqual((result["handler_calls"], result["counts"], result["saved_answer_refs"]), (1, {"acquired": 1}, []))
        replacement = self.calls[1][0]
        task = self.task("follow-on")
        self.assertNotEqual(replacement, lost)
        # Its own read: counted as a try, never mistaken for the borrowed one.
        self.assertEqual((task["acquisition_id"], task["attempt_refs"], task["tries"], task["generation"]),
                         (replacement, [replacement], 1, 1))
        self.assertNotIn("reused_acquisition_ref", task)
        self.assertEqual(self.run_follow_on(handlers)["handler_calls"], 0)

    def test_a_borrowed_lost_read_with_no_try_left_is_closed_without_a_read(self):
        handlers = self.handlers(); self.prepare(handlers); self.borrowed(handlers)
        tid = next(rid for rid, row in self.memory.snapshot()["query_tasks"].items() if row.get("run_id") == "follow-on")
        old = self.memory.snapshot()["query_tasks"][tid]
        self.memory.put("query_tasks", tid, dict(old, tries=2), previous=old)  # Its tries are used up.
        result = self.run_follow_on(handlers)
        self.assertEqual((result["handler_calls"], result["counts"]), (0, {"blocked": 1}))
        task = self.task("follow-on")
        self.assertEqual((task["status"], task["reason"], task["reconcile_required"], task["retryable"]),
                         ("blocked", "SOURCE_SAVED_READ_SET_ASIDE", False, False))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.run_follow_on(handlers)["handler_calls"], 0)
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
