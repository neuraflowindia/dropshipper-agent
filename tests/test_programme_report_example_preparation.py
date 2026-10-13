"""Report-boundary example preparation with real disposable eligible types.

The shared inputs use actual A/B, fit, market and supplier writers with local
synthetic runners. No external website, model, real research or release runs.
Only the package-execution boundary is substituted in the connectivity cases.
"""

from copy import deepcopy
from contextlib import contextmanager, ExitStack
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import product_radar.functional_types as functional_types
import product_radar.programme as programme
import test_functional_source_market as fixtures
from product_radar.fit_description import FUNCTIONAL_CONTRACT
from product_radar.fit_review import prepare_practical_fit_reviews, run_fit_reviews
from product_radar.issue_allocation import allocate_issues
from product_radar.models import SafetyError, digest, timestamp
from product_radar.report_examples import RECOMMENDATION_CONTRACT
from product_radar.report_selection import build_report_pool
from product_radar.report_workflow import freeze_review_input
from product_radar.research_memory import ResearchMemory, change
from product_radar.storage import Journal, load_json
from product_radar.work_turns import bind_work_turn, close_work_turn, open_work_turn
from test_functional_fit import FunctionalFitRunner

NOW = fixtures.NOW


class ProgrammeReportExamplePreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        # Suppress the fixture's explicit preparation, not the application
        # readers or any eligibility check. This is a valid saved prefix with
        # all exact B judgments and fit/source/supplier proof already present.
        with patch.object(fixtures, "prepare_example_memberships", return_value={}):
            memory, cls.ids = fixtures.functional_pool_fixture(Path(temporary.name), count=10)
        cls.saved = memory.snapshot()
        pool = build_report_pool(memory, as_of=NOW, recommendation_contract=RECOMMENDATION_CONTRACT)
        if len(pool["eligible"]) != 10 or any(row["example_products"] for row in pool["eligible"]):
            raise AssertionError("Expected ten genuinely eligible types awaiting deterministic examples.")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"),
                                     evidence_type="synthetic")
        self.memory.commit("join-synthetic-report-examples", [change(table, ref, row)
            for table, rows in self.saved.items() for ref, row in rows.items()])
        self.record = programme.prepare_programme(self.memory, "report-example-preparation",
            concept_ids=self.ids, source_run_id="report-example-source", as_of=NOW)
        self.context = {"at": NOW, "fixed_as_of": NOW, "root": self.root, "runner": None,
                        "stage": "reports", "retry_seconds": 300, "profile_limit": 1}
        self.pool = self.build_pool()
        self.eligible = programme._reporting_eligible(self.record, self.memory.snapshot(), self.pool)
        self.assertEqual(len(self.eligible), 10)

    def build_pool(self):
        return build_report_pool(self.memory, as_of=self.context["at"],
                                 recommendation_contract=RECOMMENDATION_CONTRACT)

    def prepare(self, eligible=None, *, state=None):
        return programme._prepare_reporting_examples(self.memory, self.record, self.context,
            state=self.memory.snapshot() if state is None else state,
            eligible=self.eligible[:2] if eligible is None else eligible)

    def run_reports(self):
        captured = []
        def package(memory, issue, facts, **options):
            if issue["ready_for_review"]:
                # Real assets, frozen input and checked hold have already been
                # prepared. Do not run narration, PDF rendering or uploads.
                captured.append((deepcopy(issue), deepcopy(facts)))
                return {"issue_id": issue["issue_id"], "revision": issue["revision"],
                    "state": "narration_pending", "reason": "REPORT_WRITING_NOT_COMPLETE",
                    "complete": False, "approved": False, "client_ready": False}
            return {"issue_id": issue["issue_id"], "state": "shortfall",
                    "shortfall": issue["shortfall"], "approved": False, "client_ready": False}
        turn = open_work_turn(self.memory, self.record["programme_id"], at=self.context["at"], report_ready=True)
        try:
            with bind_work_turn(self.memory, turn["id"]), patch(
                    "product_radar.report_workflow.prepare_review_package", side_effect=package):
                result = programme._reports(self.memory, self.record, self.context)
        finally:
            close_work_turn(self.memory, turn["id"], at=self.context["at"],
                            reason="Disposable report example test complete")
        return result, captured

    @contextmanager
    def no_report_actions(self):
        with ExitStack() as stack:
            pool = stack.enter_context(patch.object(programme, "_programme_report_pool",
                                                   wraps=programme._programme_report_pool))
            for target in ("product_radar.issue_allocation.allocate_issues",
                           "product_radar.issue_allocation.reserve_issue",
                           "product_radar.report_workflow.freeze_review_input",
                           "product_radar.programme._functional_report_revision"):
                stack.enter_context(patch(target, side_effect=AssertionError(
                    "Stopped research must not allocate, freeze, reserve or prepare report assets")))
            yield pool

    def assert_report_owner_paused(self, result, captured):
        self.assertEqual(result["state"], "owner_paused")
        self.assertEqual(result["outcomes"], [])
        self.assertEqual(result["eligible_products"], 10)
        self.assertIs(result["approved"], False)
        self.assertIs(result["client_publication_attempted"], False)
        self.assertEqual(captured, [])
        self.assertEqual(self.record["drafts"], {})
        self.assertEqual(self.record["deferrals"], {})
        self.assertFalse((self.root / "data" / "outputs").exists())
        state = self.memory.snapshot()
        self.assertEqual(state["issue_reservations"], {})
        self.assertEqual(state["published_history"], {})
        self.assertEqual(state["jobs"], self.saved["jobs"])
        self.assertEqual(state["assessments"], self.saved["assessments"])
        for row in state["batch_runs"].values():
            if row.get("kind") == "work-turn-v1":
                self.assertEqual(row["counters"]["source_callbacks"], 0)
                self.assertEqual(row["counters"]["model_executions"], 0)

    def test_reports_prepare_all_ten_before_real_frozen_input_and_checked_hold(self):
        before = self.memory.snapshot()
        self.assertTrue(all(not row["example_products"] for row in self.pool["eligible"]))
        self.assertTrue(all(row["kpi_matches"] == 10 and row["opportunity_priority"] == 1
                            and len(row["priority_support"]["sources"]) == 2 for row in self.eligible))
        with patch.object(functional_types, "prepare_example_memberships",
                          wraps=functional_types.prepare_example_memberships) as writer:
            result, captured = self.run_reports()
        self.assertEqual(writer.call_count, 10)  # Deliberately exceeds profile_limit=1.
        self.assertEqual(result["eligible_products"], 10)
        self.assertEqual(result["example_preparation"]["attempted"], 10)
        self.assertEqual(result["example_preparation"]["waiting"], [])
        self.assertEqual(len(captured), 1)
        issue, facts = captured[0]
        self.assertTrue(issue["ready_for_review"])
        self.assertEqual(len(issue["products"]), 10)
        self.assertTrue(all(len(row["example_products"]) == 1 for row in [*issue["products"], *facts]))
        frozen = load_json(self.root / "data" / "outputs" / issue["issue_id"] /
                           ("revision-" + str(issue["revision"])) / "package-input.json", 8 * 1024 * 1024)
        self.assertEqual(frozen["product_facts"], facts)
        after = self.memory.snapshot()
        self.assertEqual(after["jobs"], before["jobs"])
        self.assertEqual(after["assessments"], before["assessments"])
        self.assertEqual(after["issue_reservations"][issue["issue_id"]]["state"], "reserved")
        self.assertEqual(after["published_history"], {})
        turns = [row for row in after["batch_runs"].values() if row.get("kind") == "work-turn-v1"]
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["counters"]["source_callbacks"], 0)
        self.assertEqual(turns[0]["counters"]["model_executions"], 0)

    def test_unchanged_reinspection_does_not_write_or_repeat_model_or_assessment(self):
        first = self.prepare()
        self.assertEqual(first["attempted"], 2)
        before = self.memory.snapshot()
        with patch.object(functional_types, "prepare_example_memberships",
                          side_effect=AssertionError("Current examples must not be rewritten")):
            again = self.prepare()
        self.assertEqual(again, {"attempted": 0, "prepared": [], "waiting": []})
        self.assertEqual(self.memory.snapshot(), before)

    def test_already_stopped_helper_has_no_writer_or_canonical_change(self):
        programme.set_programme_service_state(self.memory, enabled=False, as_of=NOW)
        before = self.memory.snapshot()
        with patch.object(functional_types, "prepare_example_memberships",
                          side_effect=AssertionError("Stopped helper must not write examples")) as writer, \
                patch.object(programme, "record_error") as errors:
            result = self.prepare()
        self.assertEqual(result, {"attempted": 0, "prepared": [], "waiting": [], "state": "owner_paused"})
        writer.assert_not_called()
        errors.assert_not_called()
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(self.record["deferrals"], {})

    def test_already_stopped_report_step_does_not_allocate_or_freeze(self):
        programme.set_programme_service_state(self.memory, enabled=False, as_of=NOW)
        with self.no_report_actions() as pool, patch.object(
                functional_types, "prepare_example_memberships",
                side_effect=AssertionError("Stopped report step must not write examples")) as writer:
            result, captured = self.run_reports()
        self.assertEqual(pool.call_count, 1)
        writer.assert_not_called()
        self.assert_report_owner_paused(result, captured)
        self.assertEqual(result["example_preparation"]["attempted"], 0)
        self.assertTrue(all(functional_types.example_membership_preparation(
            self.memory.snapshot(), cid)["missing_ids"] for cid in self.ids))

    def test_stop_after_first_real_save_leaves_siblings_pending_without_report_actions(self):
        original = functional_types.prepare_example_memberships
        saved = []
        def stop_after_save(memory, cid, **options):
            result = original(memory, cid, **options)
            saved.append(cid)
            programme.set_programme_service_state(memory, enabled=False, as_of=NOW)
            return result
        with self.no_report_actions() as pool, patch.object(
                functional_types, "prepare_example_memberships", side_effect=stop_after_save) as writer, \
                patch.object(programme, "record_error") as errors:
            result, captured = self.run_reports()
        self.assertEqual(writer.call_count, 1)
        self.assertEqual(pool.call_count, 1)  # No refreshed pool after Stop.
        errors.assert_not_called()
        self.assert_report_owner_paused(result, captured)
        preparation = result["example_preparation"]
        self.assertEqual(preparation["state"], "owner_paused")
        self.assertEqual(preparation["attempted"], 1)
        self.assertEqual(len(preparation["prepared"]), 1)
        self.assertEqual(preparation["waiting"], [])
        state = self.memory.snapshot()
        self.assertTrue(functional_types.current_example_memberships(state, saved[0]))
        remaining = [cid for cid in self.ids if cid not in saved]
        self.assertEqual(len(remaining), 9)
        self.assertTrue(all(functional_types.example_membership_preparation(state, cid)["missing_ids"]
                            for cid in remaining))
        self.assertTrue(all(not functional_types.current_example_memberships(state, cid) for cid in remaining))

    def test_stop_after_final_subject_is_seen_before_refreshed_pool_or_report_actions(self):
        original = functional_types.prepare_example_memberships
        saved = []
        def stop_after_final_save(memory, cid, **options):
            result = original(memory, cid, **options)
            saved.append(cid)
            if len(saved) == len(self.ids):
                programme.set_programme_service_state(memory, enabled=False, as_of=NOW)
            return result
        with self.no_report_actions() as pool, patch.object(
                functional_types, "prepare_example_memberships", side_effect=stop_after_final_save) as writer, \
                patch.object(programme, "record_error") as errors:
            result, captured = self.run_reports()
        self.assertEqual(writer.call_count, 10)
        self.assertEqual(pool.call_count, 1)
        errors.assert_not_called()
        self.assert_report_owner_paused(result, captured)
        self.assertEqual(result["example_preparation"]["attempted"], 10)
        self.assertEqual(len(result["example_preparation"]["prepared"]), 10)
        self.assertEqual(result["example_preparation"]["waiting"], [])
        self.assertTrue(all(functional_types.current_example_memberships(self.memory.snapshot(), cid)
                            for cid in self.ids))

    def test_eligible_type_with_genuinely_no_admitted_examples_is_not_excluded(self):
        cid = self.ids[0]
        state = self.memory.snapshot()
        meaning = functional_types.effective_functional_definition(state, cid)
        definition = state["source_checks"][meaning["definition_id"]]
        children = {ref: row for ref, row in state["associations"].items() if row.get("concept_id") == cid}
        functional_types.accept_functional_definition(self.memory, cid, definition["definition"],
            basis_refs=list(definition["basis_refs"]), reviewer="synthetic", reviewed_at=NOW,
            expected_meaning_hash=meaning["meaning_hash"], example_decisions={ref: {
                "decision": "different", "reason": "Explicitly outside this fixture type."} for ref in children})
        reviews = prepare_practical_fit_reviews(self.memory, concept_ids=[cid], as_of=NOW,
                                                contract_version=FUNCTIONAL_CONTRACT)
        run_fit_reviews(self.memory, FunctionalFitRunner(), [row["review_id"] for row in reviews])
        signal = functional_types.example_membership_preparation(self.memory.snapshot(), cid)
        self.assertEqual((signal["bindings"], signal["missing_ids"], signal["pending_child_ids"]), ({}, [], []))
        result, captured = self.run_reports()
        self.assertEqual(result["eligible_products"], 10)
        self.assertEqual(result["example_preparation"]["attempted"], 9)
        self.assertFalse(result["example_preparation"]["waiting"])
        issue, facts = captured[0]
        self.assertIn(cid, {row["concept_id"] for row in issue["products"]})
        fact = next(row for row in facts if row["concept_id"] == cid)
        self.assertEqual(fact["example_products"], [])

    def test_item_safety_failure_does_not_hide_healthy_sibling(self):
        first, second = [row["concept_id"] for row in self.eligible[:2]]
        original = functional_types.prepare_example_memberships
        def prepare(memory, cid, **options):
            if cid == first:
                raise SafetyError("SYNTHETIC_EXAMPLE_WAIT")
            return original(memory, cid, **options)
        with patch.object(functional_types, "prepare_example_memberships", side_effect=prepare), \
                patch.object(programme, "record_error") as errors:
            result = self.prepare()
        self.assertEqual(result["attempted"], 2)
        self.assertEqual([row["concept_id"] for row in result["waiting"]], [first])
        self.assertEqual([row["concept_id"] for row in result["prepared"]], [second])
        self.assertTrue(functional_types.current_example_memberships(self.memory.snapshot(), second))
        self.assertEqual(errors.call_args.args[0], "report_example_preparation_failed")

    def test_file_failure_has_clear_wait_and_keeps_other_items_running(self):
        cid = self.eligible[0]["concept_id"]
        original = functional_types.prepare_example_memberships
        def prepare(memory, target, **options):
            if target == cid:
                raise OSError("Synthetic file problem")
            return original(memory, target, **options)
        with patch.object(functional_types, "prepare_example_memberships", side_effect=prepare):
            result = self.prepare()
        self.assertEqual(result["waiting"][0]["reason"], "REPORT_EXAMPLE_LOCAL_FILE_WAIT")
        self.assertEqual(len(result["prepared"]), 1)

    def test_retry_keeps_exact_binding_until_due_and_then_retries(self):
        selected = self.eligible[:1]
        cid = selected[0]["concept_id"]
        signal = functional_types.example_membership_preparation(self.memory.snapshot(), cid)
        with patch.object(functional_types, "prepare_example_memberships", side_effect=OSError("Fixture wait")) as writer:
            first = self.prepare(selected)
            deferred = deepcopy(self.record["deferrals"]["report_examples:" + cid])
            again = self.prepare(selected)
        self.assertEqual(writer.call_count, 1)
        self.assertEqual(deferred["binding"], digest(signal))
        self.assertEqual(deferred["next_retry_at"],
                         (timestamp(NOW) + timedelta(seconds=300)).isoformat())
        self.assertEqual(again["attempted"], 0)
        self.assertEqual(again["waiting"], first["waiting"])
        self.assertEqual(self.record["deferrals"]["report_examples:" + cid], deferred)
        self.context["at"] = deferred["next_retry_at"]
        retried = self.prepare(selected)
        self.assertEqual(retried["attempted"], 1)
        self.assertEqual(retried["waiting"], [])
        self.assertNotIn("report_examples:" + cid, self.record["deferrals"])

    def test_changed_exact_binding_wakes_before_old_retry_time(self):
        selected = self.eligible[:1]
        cid = selected[0]["concept_id"]
        with patch.object(functional_types, "prepare_example_memberships", side_effect=OSError("Fixture wait")):
            self.prepare(selected)
        self.record["deferrals"]["report_examples:" + cid]["binding"] = digest("different old bindings")
        with patch.object(functional_types, "prepare_example_memberships",
                          wraps=functional_types.prepare_example_memberships) as writer:
            result = self.prepare(selected)
        self.assertEqual(writer.call_count, 1)
        self.assertEqual(result["waiting"], [])

    def test_already_saved_bindings_clear_old_wait_without_writer(self):
        selected = self.eligible[:1]
        cid = selected[0]["concept_id"]
        with patch.object(functional_types, "prepare_example_memberships", side_effect=OSError("Fixture wait")):
            self.prepare(selected)
        functional_types.prepare_example_memberships(self.memory, cid, as_of=NOW)
        with patch.object(functional_types, "prepare_example_memberships",
                          side_effect=AssertionError("The completed writer must not repeat")):
            result = self.prepare(selected)
        self.assertEqual(result, {"attempted": 0, "prepared": [], "waiting": []})
        self.assertNotIn("report_examples:" + cid, self.record["deferrals"])

    def test_lost_successful_reply_recovers_only_saved_exact_bindings(self):
        selected = self.eligible[:1]
        original = functional_types.prepare_example_memberships
        def lost_reply(*args, **options):
            original(*args, **options)
            raise OSError("Synthetic lost reply after exact commit")
        before = self.memory.snapshot()
        with patch.object(functional_types, "prepare_example_memberships", side_effect=lost_reply) as writer:
            result = self.prepare(selected)
        self.assertEqual(writer.call_count, 1)
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["waiting"], [])
        self.assertTrue(result["prepared"][0]["recovered_after_error"])
        self.assertEqual(result["prepared"][0]["original_failure"], "REPORT_EXAMPLE_LOCAL_FILE_WAIT")
        self.assertEqual(self.memory.snapshot()["jobs"], before["jobs"])
        self.assertFalse(self.record["deferrals"])

    def test_reports_refresh_after_lost_reply_and_do_not_freeze_old_empty_facts(self):
        original = functional_types.prepare_example_memberships
        calls = []
        def lost_reply(memory, cid, **options):
            result = original(memory, cid, **options)
            calls.append(cid)
            if len(calls) == 1:
                raise OSError("Synthetic reply loss")
            return result
        with patch.object(functional_types, "prepare_example_memberships", side_effect=lost_reply):
            result, captured = self.run_reports()
        self.assertEqual(len(calls), 10)
        self.assertEqual(result["example_preparation"]["waiting"], [])
        self.assertTrue(any(row.get("recovered_after_error") for row in result["example_preparation"]["prepared"]))
        self.assertTrue(all(len(row["example_products"]) == 1 for row in captured[0][1]))

    def test_lost_reply_cannot_recover_after_actual_definition_meaning_changed(self):
        selected = self.eligible[:1]
        cid = selected[0]["concept_id"]
        original = functional_types.prepare_example_memberships
        def changed_before_reply(memory, target, **options):
            original(memory, target, **options)
            state = memory.snapshot()
            meaning = functional_types.effective_functional_definition(state, target)
            saved = state["source_checks"][meaning["definition_id"]]
            definition = dict(saved["definition"], mechanism="A different synthetic mechanism")
            functional_types.accept_functional_definition(memory, target, definition,
                basis_refs=list(saved["basis_refs"]), reviewer="synthetic", reviewed_at=NOW,
                expected_meaning_hash=meaning["meaning_hash"], example_decisions={})
            raise OSError("Synthetic reply loss after competing meaning change")
        with patch.object(functional_types, "prepare_example_memberships", side_effect=changed_before_reply) as writer:
            result = self.prepare(selected)
        self.assertEqual(writer.call_count, 1)
        self.assertEqual(result["prepared"], [])
        self.assertEqual([row["concept_id"] for row in result["waiting"]], [cid])
        self.assertNotEqual(functional_types.effective_functional_definition(
            self.memory.snapshot(), cid)["meaning_hash"], selected[0]["functional_definition"]["meaning_hash"])
        self.assertFalse(functional_types.current_example_memberships(self.memory.snapshot(), cid))

    def test_lost_reply_recovery_integrity_error_is_not_downgraded_to_item_wait(self):
        error = SafetyError("SYMLINK_PATH_REFUSED")
        original = functional_types.example_membership_preparation
        reads = []
        def inspect(state, cid):
            reads.append(cid)
            if len(reads) == 2:
                raise error
            return original(state, cid)
        with patch.object(functional_types, "example_membership_preparation", side_effect=inspect), \
                patch.object(functional_types, "prepare_example_memberships", side_effect=OSError("Synthetic lost reply")) as writer:
            with self.assertRaises(SafetyError) as raised:
                self.prepare()
        self.assertIs(raised.exception, error)
        self.assertEqual(len(reads), 2)
        self.assertEqual(writer.call_count, 1)
        self.assertEqual(self.record["deferrals"], {})

    def test_reports_remove_only_waiting_types_from_real_eligible_pool(self):
        failed = self.eligible[0]["concept_id"]
        original = functional_types.prepare_example_memberships
        def prepare(memory, cid, **options):
            if cid == failed:
                raise SafetyError("SYNTHETIC_EXAMPLE_WAIT")
            return original(memory, cid, **options)
        with patch.object(functional_types, "prepare_example_memberships", side_effect=prepare):
            result, captured = self.run_reports()
        self.assertEqual(result["eligible_products"], 9)
        self.assertEqual(result["example_preparation"]["attempted"], 10)
        self.assertEqual([row["concept_id"] for row in result["example_preparation"]["waiting"]], [failed])
        self.assertEqual(captured, [])  # Ten types are required for a complete issue.
        final = self.build_pool()
        self.assertEqual(len(final["eligible"]), 10)  # Wait did not falsify underlying qualification.
        self.assertTrue(all(len(row["example_products"]) == 1 for row in final["eligible"]
                            if row["concept_id"] != failed))

    def test_owner_interrupt_and_integrity_failure_propagate_without_next_item(self):
        for error in (KeyboardInterrupt(), SystemExit(130), SafetyError("RESEARCH_EVENTS_INVALID"),
                      RuntimeError("Synthetic unexpected writer defect")):
            with self.subTest(error=type(error).__name__), patch.object(
                    functional_types, "prepare_example_memberships", side_effect=error) as writer:
                with self.assertRaises(type(error)) as raised:
                    self.prepare()
            self.assertIs(raised.exception, error)
            self.assertEqual(writer.call_count, 1)
            self.assertEqual(self.record["deferrals"], {})
        self.assertFalse(any(row.get("kind") == "concept-example-v1"
                             for row in self.memory.snapshot()["source_checks"].values()))

    def test_legacy_marker_and_empty_selection_do_not_request_preparation(self):
        legacy = deepcopy(self.eligible[0]); legacy.pop("recommendation_contract")
        before = self.memory.snapshot()
        with patch.object(functional_types, "example_membership_preparation",
                          side_effect=AssertionError("Legacy/empty selections must not inspect examples")), \
                patch.object(functional_types, "prepare_example_memberships") as writer:
            for selected in ([], [legacy]):
                self.assertEqual(self.prepare(selected), {"attempted": 0, "prepared": [], "waiting": []})
        writer.assert_not_called()
        self.assertEqual(self.memory.snapshot(), before)

    def test_progress_callback_failure_cannot_block_preparation(self):
        seen = []
        def callback(row):
            seen.append(deepcopy(row))
            raise RuntimeError("Synthetic diagnostic callback failure")
        self.context["on_progress"] = callback
        with patch.object(programme, "record_error") as errors:
            result = self.prepare()
        self.assertEqual(result["attempted"], 2)
        self.assertEqual(len(result["prepared"]), 2)
        self.assertEqual(result["waiting"], [])
        self.assertEqual([row["phase_state"] for row in seen],
                         ["example_preparation_started", "example_preparation_finished"] * 2)
        self.assertEqual(errors.call_count, 4)
        self.assertTrue(all(call.args[0] == "progress_output_failed" for call in errors.call_args_list))

    def test_existing_empty_frozen_input_is_preserved_in_its_original_revision(self):
        state = self.memory.snapshot()
        allocation = allocate_issues(self.pool["eligible"], self.record["release_slots"],
            state["published_history"], state["issue_reservations"], identity_state=state)
        issue = allocation["issues"][0]
        self.assertTrue(issue["ready_for_review"])
        facts = [self.pool["facts_by_concept"][row["concept_id"]] for row in issue["products"]]
        issue, facts = programme._functional_report_revision(self.root, issue, facts, "synthetic")
        self.assertTrue(all(not row["example_products"] for row in facts))
        freeze_review_input(self.memory, issue, facts, root=self.root)
        folder = self.root / "data" / "outputs" / issue["issue_id"] / ("revision-" + str(issue["revision"]))
        original = {path.relative_to(self.root): path.read_bytes() for path in folder.rglob("*") if path.is_file()}
        result, captured = self.run_reports()
        self.assertEqual(result["example_preparation"]["attempted"], 10)
        self.assertEqual({ref: (self.root / ref).read_bytes() for ref in original}, original)
        self.assertEqual(len(captured), 1)
        self.assertGreater(captured[0][0]["revision"], issue["revision"])
        self.assertTrue(all(len(row["example_products"]) == 1 for row in captured[0][1]))


if __name__ == "__main__":
    unittest.main()
