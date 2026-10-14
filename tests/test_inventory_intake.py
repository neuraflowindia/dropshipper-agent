"""Rolling intake uses saved synthetic facts; no real source/model calls."""

from copy import deepcopy
from datetime import timedelta
import unittest
from unittest.mock import patch

from product_radar.inventory_intake import (INTAKE_KIND, WORK_KIND, inventory_work_readiness,
    refresh_inventory_intake, reporting_intake_concept_ids, validate_inventory_work, admit_selected_inventory_work)
from product_radar.models import SafetyError, digest, timestamp
from product_radar.programme import _binding, prepare_programme
import product_radar.programme as programme
from product_radar.research_memory import change, key
import test_programme as fixtures
from test_programme_summary import product


class InventoryIntakeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProgrammeTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory, self.now = self.fixture.memory, self.fixture.now
        self.origin = self.fixture.prepare()
        self.record = prepare_programme(self.memory, "current-intake", concept_ids=[], as_of=self.now)

    def pool(self, **changes):
        return {"eligible": [product("first"), product("second")], "needed_fact_work": [],
            "needed_market_work": [], "needed_supplier_work": [], **changes}

    def refresh(self, pool=None, **options):
        result = refresh_inventory_intake(self.memory, self.record, as_of=self.now,
            pool=self.pool() if pool is None else pool, **options)
        self.record.update({field: result[field] for field in ("reporting_intake_ids", "inventory_work_ids")})
        return result

    def test_current_intake_preserves_original_scope_and_creates_no_launches_or_holds(self):
        before = self.memory.snapshot(); binding = _binding(self.record)
        self.refresh()
        state = self.memory.snapshot()
        self.assertEqual(reporting_intake_concept_ids(state, self.record, pool=self.pool()), {"first", "second"})
        self.assertEqual(_binding(self.record), binding)
        self.assertEqual(self.record["concept_ids"], [])
        for table in ("query_tasks", "jobs", "captures", "issue_reservations", "published_history"):
            self.assertEqual(state[table], before[table])
        for ref in self.record["reporting_intake_ids"]:
            self.assertEqual(state["batch_runs"][ref]["binding"]["origin_scope"]["programme_id"], "whole-programme")

    def test_expired_intake_is_skipped_then_judged_again_to_a_fresh_record(self):
        from product_radar.inventory_intake import current_intake_records, expire_intake_records
        self.refresh()
        state = self.memory.snapshot()
        first = next(ref for ref in self.record["reporting_intake_ids"] if state["batch_runs"][ref]["binding"]["concept_id"] == "first")
        self.assertEqual(expire_intake_records(self.memory, [first], as_of=self.now, reason="Synthetic cutover."), [first])
        self.assertEqual(expire_intake_records(self.memory, [first], as_of=self.now, reason="Synthetic cutover."), [])
        state = self.memory.snapshot()
        self.assertEqual(state["batch_runs"][first]["expired"], {"at": self.now, "reason": "Synthetic cutover."})
        self.assertEqual({row["binding"]["concept_id"] for row in current_intake_records(state, self.record)}, {"second"})
        for reason in ("", " ", "x" * 501, None):
            with self.assertRaisesRegex(SafetyError, "INVENTORY_EXPIRY_REASON_INVALID"):
                expire_intake_records(self.memory, [first], as_of=self.now, reason=reason)
        with self.assertRaisesRegex(SafetyError, "INVENTORY_BINDING_INVALID"):
            expire_intake_records(self.memory, ["reporting-intake-missing"], as_of=self.now, reason="Synthetic.")
        # Judged again to the same binding: admitted afresh under the same id.
        self.refresh()
        state = self.memory.snapshot()
        self.assertNotIn("expired", state["batch_runs"][first])
        self.assertEqual(reporting_intake_concept_ids(state, self.record, pool=self.pool()), {"first", "second"})

    def test_unchanged_refresh_does_not_create_canonical_tail_or_new_intake(self):
        self.refresh(); before = self.memory.snapshot_with_provenance()
        self.refresh(); after = self.memory.snapshot_with_provenance()
        self.assertEqual(before, after)

    def test_partial_event_cursor_rebuilds_all_subjects_without_losing_remainder(self):
        for _ in range(20):
            result = self.refresh(subject_limit=1, event_limit=1)
            if not result["backlog_remaining"]:
                break
        self.assertFalse(result["backlog_remaining"])
        self.assertEqual(reporting_intake_concept_ids(self.memory.snapshot(), self.record), {"first", "second"})

    def test_newly_ready_older_product_enters_current_intake_after_week_preparation(self):
        pool = self.pool(eligible=[product("first")]); self.refresh(pool)
        self.assertEqual(reporting_intake_concept_ids(self.memory.snapshot(), self.record), {"first"})
        old = self.memory.snapshot()["concepts"]["second"]
        revised = deepcopy(old); revised["description"]["reason"] = "Synthetic finished practical review."
        self.memory.put("concepts", "second", revised, previous=old)
        self.refresh()
        self.assertEqual(reporting_intake_concept_ids(self.memory.snapshot(), self.record), {"first", "second"})

    def test_genuinely_accepted_updated_intake_keeps_baseline_and_rejects_bare_flag(self):
        updated = product("first", status="Updated", material_update_reviewed=True,
            material_update_review={"accepted": True, "decision": "useful_update", "baseline": {"record_id": "actual-baseline"}})
        self.refresh(self.pool(eligible=[updated]))
        row = self.memory.snapshot()["batch_runs"][self.record["reporting_intake_ids"][0]]
        self.assertEqual(row["binding"]["status"], "Updated")
        self.assertEqual(row["binding"]["publication_baseline"], {"record_id": "actual-baseline"})
        bad = product("second", status="Updated", material_update_reviewed=True)
        self.refresh(self.pool(eligible=[bad]))
        self.assertEqual(self.record["reporting_intake_ids"], [])

    def test_foreign_hold_excludes_intake_without_rewriting_hold(self):
        hold = {"client_id": "default", "issue_id": "another-issue", "state": "reserved", "products": [{"concept_id": "first"}]}
        self.memory.put("issue_reservations", "another-issue", hold)
        self.refresh()
        self.assertEqual(reporting_intake_concept_ids(self.memory.snapshot(), self.record), {"second"})
        self.assertEqual(self.memory.snapshot()["issue_reservations"]["another-issue"], hold)

    def test_report_contract_pending_is_local_wait_not_another_fit_or_source_job(self):
        self.refresh(self.pool(eligible=[], needed_fact_work=[{"concept_id": "first",
            "code": "functional_report_contract_required", "reason": "Matching format not connected.", "blocking": True}]))
        state = self.memory.snapshot(); row = validate_inventory_work(state["batch_runs"][self.record["inventory_work_ids"][0]])
        self.assertEqual(row["binding"]["purpose"], "report_contract")
        self.assertEqual(row["progress"]["reason"], "report_contract_pending")
        self.assertFalse(inventory_work_readiness(state, self.record, as_of=self.now)["ready"])
        self.assertEqual(state["jobs"], {}); self.assertEqual(state["query_tasks"], {})

    def test_ordinary_unfinished_fit_has_one_obligation_across_consumers_and_deadline(self):
        pool = self.pool(eligible=[], needed_fact_work=[{"concept_id": "first", "code": "checklist_review_required",
            "reason": "No current review.", "blocking": True}])
        self.refresh(pool); first_ids = self.record["inventory_work_ids"][:]
        second_record = prepare_programme(self.memory, "another-consumer", concept_ids=[], as_of=self.now)
        second = refresh_inventory_intake(self.memory, second_record, as_of=self.now, pool=pool)
        self.assertEqual(second["inventory_work_ids"], first_ids)
        ref = first_ids[0]; state = self.memory.snapshot(); old = state["batch_runs"][ref]
        future = (timestamp(self.now) + timedelta(hours=1)).isoformat()
        changed = deepcopy(old); changed["progress"].update(state="waiting", next_retry_at=future)
        self.memory.put("batch_runs", ref, changed, previous=old)
        state = self.memory.snapshot()
        self.assertFalse(inventory_work_readiness(state, self.record, as_of=self.now)["ready"])
        self.assertEqual([row["id"] for row in inventory_work_readiness(state, self.record, as_of=future)["ready"]], first_ids)

    def test_canonical_binding_corruption_does_not_become_empty_inventory(self):
        self.refresh(); state = self.memory.snapshot(); ref = self.record["reporting_intake_ids"][0]
        changed = deepcopy(state); changed["batch_runs"][ref]["binding"]["meaning_hash"] = "f" * 64
        with self.assertRaisesRegex(SafetyError, "INVENTORY_BINDING_INVALID"):
            reporting_intake_concept_ids(changed, self.record)

    def test_bad_work_progress_and_bad_completed_cursor_do_not_silently_skip_stock(self):
        pool = self.pool(eligible=[], needed_fact_work=[{"concept_id": "first", "code": "checklist_review_required",
            "reason": "No current review.", "blocking": True}])
        self.refresh(pool); state = self.memory.snapshot()
        row = deepcopy(state["batch_runs"][self.record["inventory_work_ids"][0]])
        row["progress"]["next_retry_at"] = "not-a-date"
        with self.assertRaisesRegex(SafetyError, "INVENTORY_BINDING_INVALID"):
            validate_inventory_work(row)
        control_id = key("inventory-cursor", "default")
        old = state["batch_runs"][control_id]; bad = deepcopy(old)
        bad["cursor"]["head"] = "f" * 64
        self.memory.put("batch_runs", control_id, bad, previous=old)
        with self.assertRaisesRegex(SafetyError, "INVENTORY_CURSOR_INVALID"):
            self.refresh(pool)

    def test_expired_completed_stock_does_not_create_an_automatic_refresh(self):
        self.refresh(self.pool(eligible=[], needed_market_work=[{"concept_id": "first", "code": "market_coverage_required",
            "reason": "Earlier evidence aged.", "blocking": True}]))
        rows = [row for row in self.memory.snapshot()["batch_runs"].values() if row.get("kind") == WORK_KIND]
        self.assertTrue(rows)
        self.assertTrue(all(row["binding"]["purpose"] == "local_repair" for row in rows))
        self.assertEqual(self.memory.snapshot()["query_tasks"], {})

    def test_existing_turn_finishes_older_fit_without_reopening_origin_or_copying_pool(self):
        from test_fit_description import DescriptionRunner
        from test_research_flow import capture
        capture(self.memory)
        observed = next(iter(self.memory.snapshot()["observations"].values()))
        self.memory.put("associations", "saved-first-example", {"concept_id": "first",
            "semantic_id": observed["semantic_id"], "status": "resolved"})
        original = programme.load_programme(self.memory, "whole-programme")
        original["status"] = "research_pool_finished"; programme._save(self.memory, original)
        original_binding, original_slots = _binding(original), deepcopy(original["release_slots"])
        pool = self.pool(eligible=[], needed_fact_work=[{"concept_id": "first", "code": "checklist_review_required",
            "reason": "No current review.", "blocking": True}])
        steps = self.fixture.noops(); steps.pop("fit"); runner = DescriptionRunner()
        with patch.object(programme, "_programme_report_pool", return_value=pool):
            result = programme.advance_ready_programme_turn(self.memory, root=self.fixture.root,
                programme_id="current-intake", handlers={}, steps=steps, runner=runner, as_of=self.now)
        current = programme.load_programme(self.memory, "current-intake")
        state = self.memory.snapshot()
        self.assertEqual(current["concept_ids"], [])
        self.assertEqual(_binding(programme.load_programme(self.memory, "whole-programme")), original_binding)
        self.assertEqual(programme.load_programme(self.memory, "whole-programme")["release_slots"], original_slots)
        work = [state["batch_runs"][ref] for ref in current["inventory_work_ids"]]
        self.assertEqual([row["progress"]["state"] for row in work], ["complete"])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(result["work_turn"]["counters"]["model_executions"], 1)
        self.assertEqual(state["issue_reservations"], {})

    def test_finished_origin_current_intake_and_counts_use_same_new_ids(self):
        from product_radar.programme_summary import _scopes
        with patch.object(programme, "_programme_report_pool", return_value=self.pool()):
            programme.advance_ready_programme_turn(self.memory, root=self.fixture.root,
                programme_id="current-intake", handlers={}, steps=self.fixture.noops(), as_of=self.now)
        current = programme.load_programme(self.memory, "current-intake"); state = self.memory.snapshot()
        self.assertEqual(programme._reporting_ids(current, state), ["first", "second"])
        self.assertEqual(set(_scopes(current, state)[2]), {"first", "second"})
        self.assertEqual(programme.programme_progress(self.memory, current["programme_id"], record=current,
            state=state, handlers={}, as_of=self.now)["reporting_intake_products"], 2)

    def test_new_foreign_hold_invalidates_current_intake_at_selection_without_rewrite(self):
        self.refresh()
        self.memory.put("issue_reservations", "later-hold", {"client_id": "default", "issue_id": "later-hold",
            "state": "reserved", "products": [{"concept_id": "first"}]})
        rows = programme._reporting_eligible(self.record, self.memory.snapshot(), self.pool())
        self.assertEqual([row["concept_id"] for row in rows], ["second"])

    def test_report_contract_wait_settles_without_model_or_source_work(self):
        pool = self.pool(eligible=[], needed_fact_work=[{"concept_id": "first", "code": "functional_report_contract_required",
            "reason": "Packet format not connected.", "blocking": True}])
        with patch.object(programme, "_programme_report_pool", return_value=pool):
            first = programme.advance_ready_programme_turn(self.memory, root=self.fixture.root,
                programme_id="current-intake", handlers={}, steps=self.fixture.noops(), as_of=self.now)
            for _ in range(3):
                last = programme.advance_ready_programme_turn(self.memory, root=self.fixture.root,
                    programme_id="current-intake", handlers={}, steps=self.fixture.noops(), as_of=self.now)
        self.assertTrue(first["round_completed"])
        self.assertEqual(last["state"], "all_waiting")
        self.assertEqual(self.memory.snapshot()["jobs"], {})
        self.assertEqual(self.memory.snapshot()["query_tasks"], {})


class InventoryDueIntegrationTests(unittest.TestCase):
    def setUp(self):
        import test_functional_source_interpretation as source_fixture
        from product_radar.market_validation import functional_market_context, functional_priority_support
        from product_radar.fit_policy import FIT_POLICY_ID
        self.fixture = source_fixture.FunctionalSourceInterpretationTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory = self.fixture.memory
        self.now = source_fixture.NOW
        self.fixture.review(source_fixture.Runner(judgment="reasonably_positive"))
        state = self.memory.snapshot()
        context = functional_market_context(state, "first", as_of=self.now)
        support = functional_priority_support(state, context, {"fit_policy_id": FIT_POLICY_ID,
            "kpi_total": 10, "kpi_matches": 6}, as_of=self.now)
        self.due = support["support_valid_until"]
        self.product = product("first", usable_until=self.due, **support)
        self.pool = {"eligible": [self.product], "needed_fact_work": [], "needed_market_work": [], "needed_supplier_work": []}
        self.origin = prepare_programme(self.memory, "origin", concept_ids=["first"], source_run_id="full-list", as_of=self.now)
        self.record = prepare_programme(self.memory, "next-issue", concept_ids=[], week="2026-10-19", as_of=self.now)
        self.root = self.memory.root.parent

    def test_completed_market_interpretation_is_not_called_unfinished_or_refreshed_without_selection(self):
        pool = {**self.pool, "eligible": [], "needed_market_work": [{"concept_id": "first",
            "code": "market_coverage_required", "reason": "Not enough encouraging current evidence.", "blocking": True}]}
        before = deepcopy(self.memory.snapshot()["query_tasks"])
        result = refresh_inventory_intake(self.memory, self.record, as_of=self.now, pool=pool)
        self.assertEqual(result["inventory_work_ids"], [])
        self.assertEqual(self.memory.snapshot()["query_tasks"], before)

    def test_acquired_uninterpreted_source_enters_match_lane_without_collecting_again(self):
        import test_functional_source_interpretation as source_fixture
        fresh = source_fixture.FunctionalSourceInterpretationTests(); fresh.setUp()
        self.addCleanup(fresh.doCleanups)
        memory = fresh.memory; before = deepcopy(memory.snapshot()["query_tasks"])
        prepare_programme(memory, "old-unfinished", concept_ids=["first"], source_run_id="full-list", as_of=self.now)
        consumer = prepare_programme(memory, "new-consumer", concept_ids=[], as_of=self.now)
        pool = {"eligible": [], "needed_fact_work": [], "needed_supplier_work": [], "needed_market_work": [
            {"concept_id": "first", "code": "market_coverage_required", "reason": "Saved page needs its type comparison.", "blocking": True}]}
        prepared = refresh_inventory_intake(memory, consumer, as_of=self.now, pool=pool)
        work = memory.snapshot()["batch_runs"][prepared["inventory_work_ids"][0]]
        self.assertEqual(work["binding"]["purpose"], "match")
        self.assertTrue(work["progress"]["source_collected"])
        runner = source_fixture.Runner()
        with patch.object(programme, "_programme_report_pool", return_value=pool):
            result = programme.advance_ready_programme_turn(memory, root=memory.root.parent, programme_id="new-consumer",
                handlers={}, steps=fixtures.ProgrammeTests().noops(), runner=runner, as_of=self.now)
        self.assertEqual(result["work_turn"]["counters"]["source_callbacks"], 0)
        self.assertEqual(result["work_turn"]["counters"]["model_executions"], 1)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(memory.snapshot()["query_tasks"], before)
        self.assertEqual(memory.snapshot()["batch_runs"][work["id"]]["progress"]["state"], "complete")

    def test_functional_selected_expiry_uses_actual_witness_not_legacy_target_promotion(self):
        from product_radar.source_coverage import prepare_inventory_source_repair
        before = self.memory.snapshot()
        result = admit_selected_inventory_work(self.memory, self.record, as_of=self.now, pool=self.pool)
        self.assertEqual(len(result["inventory_work_ids"]), 1)
        row = self.memory.snapshot()["batch_runs"][result["inventory_work_ids"][0]]
        self.assertEqual(row["binding"]["source"]["original_task_id"], self.fixture.task_id)
        self.assertEqual(row["progress"]["next_retry_at"], self.due)
        self.assertEqual(before["query_tasks"][self.fixture.task_id]["target_results"]["first"]["state"], "interpretation_pending")
        future = prepare_inventory_source_repair(self.memory, row, as_of=self.now)
        self.assertEqual((future["state"], future["next_retry_at"]), ("waiting", self.due))
        self.assertEqual(self.memory.snapshot()["query_tasks"], before["query_tasks"])
        self.record["inventory_work_ids"] = result["inventory_work_ids"]
        self.assertFalse(inventory_work_readiness(self.memory.snapshot(), self.record, as_of=self.now)["ready"])
        self.assertEqual(len(inventory_work_readiness(self.memory.snapshot(), self.record, as_of=self.due)["ready"]), 1)
        ready = programme.programme_turn_readiness(self.memory, self.record, root=self.root,
            handlers={"amazon-bestsellers": lambda _context: None}, as_of=self.due)
        self.assertTrue(ready["ready_lanes"]["backlog"])

    def test_two_consumers_join_one_due_owner_and_protected_slots_are_not_refreshed(self):
        first = admit_selected_inventory_work(self.memory, self.record, as_of=self.now, pool=self.pool)
        second = prepare_programme(self.memory, "same-next-issue", concept_ids=[], week="2026-10-19", as_of=self.now)
        joined = admit_selected_inventory_work(self.memory, second, as_of=self.now, pool=self.pool)
        self.assertEqual(joined["inventory_work_ids"], first["inventory_work_ids"])
        for slot in self.record["release_slots"]:
            self.memory.put("issue_reservations", slot["issue_id"], {"client_id": "default", "issue_id": slot["issue_id"],
                "state": "reserved", "products": [{"concept_id": "first"}]})
        before = self.memory.snapshot_with_provenance()
        protected = admit_selected_inventory_work(self.memory, second, as_of=self.now, pool=self.pool)
        self.assertEqual(protected["upcoming_slots"], [])
        self.assertEqual(self.memory.snapshot_with_provenance(), before)

    def test_due_original_reaches_existing_work_turn_once_without_reopening_old_scope(self):
        programme.refresh_reporting_intake(self.memory, self.record, as_of=self.now, pool=self.pool)
        programme.admit_due_inventory_work(self.memory, self.record, as_of=self.now, pool=self.pool)
        programme._save(self.memory, self.record)
        later = (timestamp(self.due) + timedelta(hours=1)).isoformat()
        old_scope = _binding(self.origin); calls = []
        def readable_empty(context):
            calls.append(context["acquisition_id"]); context["record_dispatch"]()
            return {"schema_version": 1, "route": context["route"], "acquisition_id": context["acquisition_id"],
                "observed_at": later, "state": "no_results", "dispatch_started": True,
                "reference": "synthetic-original/receipt.json", "capture_ids": [], "observation_ids": [],
                "target_results": [], "reason": "Synthetic readable empty result.", "retryable": False}
        empty_pool = {**self.pool, "eligible": [], "needed_market_work": [{"concept_id": "first",
            "code": "market_coverage_required", "reason": "Selected support aged.", "blocking": True}]}
        fixture = fixtures.ProgrammeTests()
        with patch.object(programme, "_programme_report_pool", return_value=empty_pool):
            result = programme.advance_ready_programme_turn(self.memory, root=self.root, programme_id="next-issue",
                handlers={"amazon-bestsellers": readable_empty}, steps=fixture.noops(), as_of=later)
            programme.advance_ready_programme_turn(self.memory, root=self.root, programme_id="next-issue",
                handlers={"amazon-bestsellers": readable_empty}, steps=fixture.noops(), as_of=later)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["work_turn"]["counters"]["source_callbacks"], 1)
        self.assertEqual(result["work_turn"]["counters"]["model_executions"], 0)
        self.assertEqual(_binding(programme.load_programme(self.memory, "origin")), old_scope)
        self.assertEqual(self.memory.snapshot()["issue_reservations"], {})


if __name__ == "__main__":
    unittest.main()
