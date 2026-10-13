"""Connected dispatch/feedback rehearsals use isolated synthetic records."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.models import SafetyError, digest
from product_radar.fit_policy import ACTIVE_CRITERION_IDS, FIT_POLICY_ID, LEGACY_POLICY_ID
from product_radar.query_dispatch import find_existing_task
from product_radar.research_flow import exclude_established_brand, run_flow
from product_radar.research_memory import ResearchMemory, import_capture, key
from product_radar.research_operations import sync_query_feedback
from product_radar.providers.apify import map_meta_rows
from product_radar.storage import Journal, atomic_json
from test_research_flow import FakeRunner, rows
from test_research_planner import catalogue, NOW
from product_radar.research_planner import plan_searches
from product_radar.service import policy_for_fit


class QueryDispatchTests(unittest.TestCase):
    def setUp(self):
        # This class deliberately keeps the historical thirteen-row judgments.
        # Current-policy feedback is covered independently below.
        policy = policy_for_fit(LEGACY_POLICY_ID)
        policy_patch = patch("product_radar.service.load_policy", return_value=policy)
        policy_patch.start()
        self.addCleanup(policy_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.action = plan_searches(catalogue(), [], as_of=NOW, slots=1)["actions"][0]
        self.task = {"family_id": self.action["family_id"], "request": self.action["request"], "attempt_id": "fixture-attempt",
                     "plan_id": "plan-old", "status": "prepared", "baseline_concepts": [], "collector_dispatched": False,
                     "config_hash": "a" * 64}
        self.memory.put("query_tasks", "task-one", self.task)
        self.attempt = {"submission": "submitted", "remote_status": "SUCCEEDED", "charge": "0.01", "read_charge": None,
                        "read_hold": "0.02", "capture": None, "capture_status": "captured", "dataset_id": "fixture-dataset",
                        "controls_ok": True, "config_hash": "a" * 64}

    def financial(self):
        return patch.object(self.memory.journal, "state", return_value=({}, {"fixture-attempt": deepcopy(self.attempt)}))

    def committed_capture(self, *, at=NOW, mapped=None, values=None, complete=True, truncated=False):
        values = rows() if values is None else values
        mapped = map_meta_rows(values, purpose="discovery", query_reference="fixture") if mapped is None else mapped
        self.attempt["capture"] = digest(values)
        folder = self.memory.journal.root / "fixture-attempt"
        atomic_json(folder / "captured_rows.json", values)
        atomic_json(folder / "capture_manifest.json", {"captured_hash": digest(values), "dataset_id": "fixture-dataset",
            "observed_at": at, "bounded_capture_complete": complete, "truncated": truncated})
        import_capture(self.memory, source="meta", acquisition_id="fixture-dataset", observed_at=at,
                       mapped=mapped, capture_hash=digest(values), reference={"attempt_id": "fixture-attempt"})

    def test_recreated_plan_reuses_active_exact_preparation(self):
        with patch.object(self.memory.journal, "state", return_value=({}, {})):
            result = find_existing_task(self.memory, self.action, as_of=NOW)
        self.assertEqual(result["task_id"], "task-one")
        self.assertEqual(result["reuse_kind"], "active_task")
        self.assertEqual(result["plan_id"], "plan-old")

    def test_unknown_old_submission_is_reconciled_not_replaced(self):
        self.attempt.update(submission="uncertain", remote_status=None, charge=None)
        with self.financial():
            result = find_existing_task(self.memory, self.action, as_of="2027-01-01T10:00:00+00:00")
        self.assertEqual(result["reuse_kind"], "active_task")
        self.assertIn("reconcile", result["reuse_reason"])

    def test_same_request_different_family_reuses_original_preparation(self):
        different = {**self.action, "family_id": "different-family"}
        with patch.object(self.memory.journal, "state", return_value=({}, {})):
            result = find_existing_task(self.memory, different, as_of=NOW)
        self.assertEqual(result["task_id"], "task-one")
        self.assertEqual(result["family_id"], self.task["family_id"])
        self.assertEqual(result["reuse_kind"], "active_task")

    def test_different_family_reuses_fresh_capture_without_changing_provenance(self):
        self.committed_capture()
        before = self.memory.snapshot()
        different = {**self.action, "family_id": "different-family"}
        with self.financial():
            result = find_existing_task(self.memory, different, as_of=NOW)
        self.assertEqual(result["reuse_kind"], "fresh_capture")
        self.assertEqual(result["family_id"], self.task["family_id"])
        self.assertEqual(self.memory.snapshot(), before)

    def test_different_family_unknown_submission_reconciles_original_attempt(self):
        self.attempt.update(submission="uncertain", remote_status=None, charge=None)
        different = {**self.action, "family_id": "different-family"}
        with self.financial():
            result = find_existing_task(self.memory, different, as_of="2027-01-01T10:00:00+00:00")
        self.assertEqual(result["reuse_kind"], "active_task")
        self.assertEqual(result["attempt_id"], self.task["attempt_id"])

    def test_changed_coverage_country_options_or_term_are_not_same_request(self):
        for field, value in (("coverage", {"limit": 50}), ("term", "Different useful search"),
                             ("options", {"different": True}), ("build", "new-build"),
                             ("language", "hi-Latn"), ("purpose", "validation"),
                             ("source", "marketplace"), ("date_scope", {"kind": "different_scope"})):
            changed = deepcopy(self.action)
            changed["family_id"] = "different-family"
            changed["request"][field] = value
            with self.financial():
                self.assertIsNone(find_existing_task(self.memory, changed, as_of=NOW))

    def test_complete_current_capture_reused_without_new_dates_or_cost_claim(self):
        self.committed_capture()
        with self.financial():
            result = find_existing_task(self.memory, self.action, as_of=NOW)
        self.assertEqual(result["reuse_kind"], "fresh_capture")
        self.assertNotIn("cost_usd", result)
        self.assertNotIn("observed_at", result)
        self.assertEqual(next(iter(self.memory.snapshot()["captures"].values()))["observed_at"], NOW)

    def test_saved_output_waiting_import_is_reused_for_import_first(self):
        self.attempt["capture"] = "b" * 64
        with self.financial():
            result = find_existing_task(self.memory, self.action, as_of=NOW)
        self.assertEqual(result["reuse_kind"], "active_task")
        self.assertIn("Import", result["reuse_reason"])

    def test_successful_run_waiting_capture_is_not_reposted(self):
        self.attempt.update(capture_status="not_captured")
        with self.financial():
            result = find_existing_task(self.memory, self.action, as_of=NOW)
        self.assertEqual(result["reuse_kind"], "active_task")
        self.assertIn("Resume capture", result["reuse_reason"])

    def test_stale_completed_capture_and_missing_date_not_cache(self):
        self.committed_capture(at="2026-09-01T10:00:00+00:00")
        with self.financial():
            self.assertIsNone(find_existing_task(self.memory, self.action, as_of=NOW))
        state = self.memory.snapshot()
        capture_id, captured = next(iter(state["captures"].items()))
        updated = deepcopy(captured)
        updated.pop("observed_at")
        # Controlled fake snapshot exercises an incomplete older record without
        # mutating immutable imported capture facts.
        state["captures"][capture_id] = updated
        with self.financial(), patch.object(self.memory, "snapshot", return_value=state):
            self.assertIsNone(find_existing_task(self.memory, self.action, as_of=NOW))

    def test_partial_truncated_corrupt_or_control_defective_capture_not_cache(self):
        self.committed_capture(truncated=True)
        with self.financial():
            self.assertIsNone(find_existing_task(self.memory, self.action, as_of=NOW))
        self.committed_capture()
        self.attempt["controls_ok"] = False
        with self.financial():
            self.assertIsNone(find_existing_task(self.memory, self.action, as_of=NOW))
        self.attempt["controls_ok"] = True
        atomic_json(self.memory.journal.root / "fixture-attempt" / "captured_rows.json", [{"tampered": True}])
        with self.financial():
            self.assertIsNone(find_existing_task(self.memory, self.action, as_of=NOW))

    def test_pending_interpretation_has_unknown_not_zero_novelty(self):
        self.committed_capture()
        with self.financial():
            ids = sync_query_feedback(self.memory)
        outcome = self.memory.snapshot()["query_outcomes"][ids[0]]
        self.assertEqual(outcome["pending_count"], 4)
        self.assertFalse(outcome["reviewed"])
        self.assertIsNone(outcome["unseen_promising"])
        self.assertIsNone(outcome["cost_usd"])

    def test_actual_feedback_frozen_baseline_and_unknown_read_hold(self):
        self.committed_capture()
        summary = run_flow(self.memory, FakeRunner(), as_of=NOW)
        known = summary["products"][0]["id"]
        old = self.memory.snapshot()["query_tasks"]["task-one"]
        self.memory.put("query_tasks", "task-one", {**old, "baseline_concepts": [known]}, previous=old)
        with self.financial():
            ids = sync_query_feedback(self.memory)
            second = sync_query_feedback(self.memory)
        outcome = self.memory.snapshot()["query_outcomes"][ids[0]]
        self.assertEqual(ids, second)
        self.assertEqual(outcome["resolved_count"], 2)
        self.assertEqual(outcome["known_count"], 1)
        self.assertTrue(outcome["unseen_promising"])
        self.assertTrue(outcome["reviewed"])
        self.assertIsNone(outcome["cost_usd"])

    def test_known_actor_plus_confirmed_read_cost_is_exact_not_duplicate(self):
        self.committed_capture()
        self.attempt["read_charge"] = "0.003"
        with self.financial():
            ids = sync_query_feedback(self.memory)
        self.assertEqual(self.memory.snapshot()["query_outcomes"][ids[0]]["cost_usd"], "0.013")

    def test_stale_assessment_is_not_completed_review_feedback(self):
        self.committed_capture()
        runner = FakeRunner()
        run_flow(self.memory, runner, limit=1, as_of=NOW)
        def reject_updated_caddy(role, payload, folder):
            response, usage = runner(role, payload, folder)
            if role == "E":
                for item, value in zip(payload["items"], response["results"]):
                    if item["description"]["label"] == "Sink caddy":
                        value["criteria"][0]["references"] = ["invented-observation"]
            return response, usage
        reject_updated_caddy.identity = runner.identity
        run_flow(self.memory, reject_updated_caddy, as_of=NOW)
        with self.financial():
            ids = sync_query_feedback(self.memory)
        measured = self.memory.snapshot()["query_outcomes"][ids[0]]
        self.assertEqual(measured["pending_count"], 0)
        self.assertEqual(measured["resolved_count"], 2)
        self.assertFalse(measured["reviewed"])
        self.assertIsNone(measured["unseen_promising"])
        from product_radar.report_selection import build_report_pool
        pool = build_report_pool(self.memory, as_of=NOW)
        caddy = next(i for i, c in self.memory.snapshot()["concepts"].items()
                     if c["description"]["label"] == "Sink caddy")
        self.assertIn("checklist_review_required", next(d for d in pool["decisions"] if d["concept_id"] == caddy)["codes"])
        from product_radar.checklist_repair import repair_checklist_item
        failure_id, failure = next((i, p) for i, p in self.memory.snapshot()["problems"].items()
                                   if p["stage"] == "assessment" and p["affected_id"] == caddy)
        repaired = repair_checklist_item(self.memory, runner, concept_id=caddy,
                                         failure_id=failure_id, job_id=failure["job_id"])
        self.assertEqual(repaired["status"], "accepted")
        with self.financial():
            recovered_ids = sync_query_feedback(self.memory)
        self.assertTrue(self.memory.snapshot()["query_outcomes"][recovered_ids[0]]["reviewed"])

    def test_known_concept_with_changed_checklist_input_remains_unreviewed(self):
        self.committed_capture()
        run_flow(self.memory, FakeRunner(), as_of=NOW)
        state = self.memory.snapshot()
        old = state["query_tasks"]["task-one"]
        self.memory.put("query_tasks", "task-one", dict(old, baseline_concepts=list(state["concepts"])), previous=old)
        concept_id, assessment = next(iter(state["assessments"].items()))
        self.memory.put("assessments", concept_id, dict(assessment, input_hash="a" * 64), previous=assessment)
        with self.financial():
            ids = sync_query_feedback(self.memory)
        measured = self.memory.snapshot()["query_outcomes"][ids[0]]
        self.assertEqual(measured["known_count"], 2)
        self.assertEqual(measured["pending_count"], 0)
        self.assertFalse(measured["reviewed"])
        self.assertIsNone(measured["unseen_promising"])

    def test_source_reviewed_brand_exclusions_finish_feedback_without_promising_yield(self):
        self.committed_capture()
        runner = FakeRunner()
        run_flow(self.memory, runner, as_of=NOW)
        before = self.memory.snapshot()
        for concept_id, concept in before["concepts"].items():
            exclude_established_brand(self.memory, {
                "concept_id": concept_id, "expected_description_hash": digest(concept["description"]),
                "reason": "Synthetic source review identifies an established brand.",
                "source_url": "https://example.com/brands", "source_excerpt": "Synthetic established-brand listing.",
                "reviewer": "test_operator", "reviewed_at": NOW})
        calls = len(runner.calls)
        run_flow(self.memory, runner, as_of=NOW)
        self.assertEqual(len(runner.calls), calls)
        with self.financial():
            ids = sync_query_feedback(self.memory)
        state = self.memory.snapshot()
        measured = state["query_outcomes"][ids[0]]
        self.assertTrue(measured["reviewed"])
        self.assertEqual(measured["pending_count"], 0)
        self.assertEqual(measured["resolved_count"], 2)
        self.assertFalse(measured["unseen_promising"])
        self.assertFalse(measured["useful_progress"])
        self.assertEqual(state["assessments"], before["assessments"])
        self.assertEqual(state["associations"], before["associations"])
        from product_radar.report_selection import build_report_pool
        self.assertEqual(build_report_pool(self.memory, as_of=NOW)["eligible"], [])

    def test_terminal_status_exclusions_complete_even_without_current_assessments(self):
        self.committed_capture()
        runner = FakeRunner()
        run_flow(self.memory, runner, as_of=NOW)
        initial = self.memory.snapshot()
        for concept_id, assessment in initial["assessments"].items():
            self.memory.put("assessments", concept_id, dict(assessment, input_hash="b" * 64), previous=assessment)
        for status in ("excluded", "merged", "retired"):
            with self.subTest(status=status):
                state = self.memory.snapshot()
                for concept_id, concept in state["concepts"].items():
                    self.memory.put("concepts", concept_id, dict(concept, status=status), previous=concept)
                calls = len(runner.calls)
                run_flow(self.memory, runner, as_of=NOW)
                self.assertEqual(len(runner.calls), calls)
                with self.financial():
                    ids = sync_query_feedback(self.memory)
                measured = self.memory.snapshot()["query_outcomes"][ids[0]]
                self.assertTrue(measured["reviewed"])
                self.assertFalse(measured["unseen_promising"])
                self.assertFalse(measured["useful_progress"])
                from product_radar.report_selection import build_report_pool
                self.assertEqual(build_report_pool(self.memory, as_of=NOW)["eligible"], [])

    def test_failed_source_is_not_zero_useful_or_novelty_result(self):
        self.committed_capture(values=[], mapped={"records": [], "rejected": [], "status": "source_failure"})
        with self.financial():
            ids = sync_query_feedback(self.memory)
        outcome = self.memory.snapshot()["query_outcomes"][ids[0]]
        self.assertEqual(outcome["status"], "failed")
        self.assertFalse(outcome["reviewed"])
        self.assertIsNone(outcome["unseen_promising"])
        self.assertIsNone(outcome["useful_progress"])

    def test_current_description_children_only_obsolete_associations_do_not_count(self):
        self.committed_capture()
        run_flow(self.memory, FakeRunner(), as_of=NOW)
        state = self.memory.snapshot()
        semantic_id, description = next((i, d) for i, d in state["descriptions"].items() if d["result"]["products"])
        old_child = key("child", [semantic_id, 0, digest(description["result"])])
        old_association = state["associations"][old_child]
        changed = deepcopy(description)
        changed["result"]["reason"] = "Current revised interpretation."
        self.memory.put("descriptions", semantic_id, changed, previous=description)
        new_child = key("child", [semantic_id, 0, digest(changed["result"])])
        self.memory.put("associations", new_child, deepcopy(old_association))
        with self.financial():
            ids = sync_query_feedback(self.memory)
        outcome = self.memory.snapshot()["query_outcomes"][ids[0]]
        self.assertEqual(outcome["pending_count"], 0)
        self.assertEqual(outcome["resolved_count"], 2)

    def test_capture_hash_mismatch_is_not_imported_as_feedback(self):
        self.committed_capture()
        self.attempt["capture"] = "a" * 64
        with self.financial(), self.assertRaisesRegex(SafetyError, "QUERY_FEEDBACK_CAPTURE_MISMATCH"):
            sync_query_feedback(self.memory)

    def test_task_financial_scope_mismatch_cannot_reuse_saved_job(self):
        self.attempt["config_hash"] = "b" * 64
        with self.financial(), self.assertRaisesRegex(SafetyError, "QUERY_TASK_ATTEMPT_SCOPE_MISMATCH"):
            find_existing_task(self.memory, self.action, as_of=NOW)

    def test_manifest_date_mismatch_cannot_refresh_feedback(self):
        self.committed_capture()
        atomic_json(self.memory.journal.root / "fixture-attempt" / "capture_manifest.json",
                    {"captured_hash": self.attempt["capture"], "dataset_id": "fixture-dataset",
                     "observed_at": "2026-10-06T11:00:00+00:00", "bounded_capture_complete": True, "truncated": False})
        with self.financial(), self.assertRaisesRegex(SafetyError, "QUERY_FEEDBACK_CAPTURE_MISMATCH"):
            sync_query_feedback(self.memory)


class CurrentFitRunner(FakeRunner):
    """Fictional judgments follow the real ten-row contract, not old totals."""

    identity = "synthetic-query-fit10-v1"

    def __init__(self, matches=5, *, negative=False):
        super().__init__()
        self.matches, self.negative = matches, negative

    def __call__(self, role, payload, folder):
        response, usage = super().__call__(role, payload, folder)
        if role == "E":
            for item, result in zip(payload["items"], response["results"]):
                match_count = (self.matches[item["description"]["label"]]
                               if isinstance(self.matches, dict) else self.matches)
                criteria = [row for row in result["criteria"]
                            if row["id"] in ACTIVE_CRITERION_IDS]
                for index, row in enumerate(criteria):
                    row["state"] = ("Meets" if index < match_count else
                                    "Does not meet" if self.negative else "Unclear")
                result["criteria"] = criteria
                result["audience_positioning"] = {
                    "positioning": "women_centric" if criteria[-1]["state"] == "Meets" else "unclear",
                    "reason": "Fictional buyer positioning, not a measured audience share."}
        return response, usage


class CurrentQueryFeedbackTests(unittest.TestCase):
    """Current yield feedback uses five supported matches and keeps unknowns."""

    financial = QueryDispatchTests.financial
    committed_capture = QueryDispatchTests.committed_capture

    def setUp(self):
        QueryDispatchTests.setUp(self)
        policy = policy_for_fit(FIT_POLICY_ID)
        policy_patch = patch("product_radar.service.load_policy", return_value=policy)
        policy_patch.start()
        self.addCleanup(policy_patch.stop)

    def feedback(self):
        with self.financial():
            ids = sync_query_feedback(self.memory, as_of=NOW)
        self.assertEqual(len(ids), 1)
        return self.memory.snapshot()["query_outcomes"][ids[0]]

    def test_good_and_decent_both_count_as_promising_without_new_collection(self):
        self.committed_capture()
        runner = CurrentFitRunner({"Sink caddy": 6, "Powered cleaning brush": 5})
        summary = run_flow(self.memory, runner, as_of=NOW, fit_policy_id=FIT_POLICY_ID)
        self.assertEqual({row["fit_tier"] for row in summary["products"]}, {"good", "decent"})
        self.assertEqual({row["checklist_meets"] for row in summary["products"]}, {5, 6})
        self.assertEqual(summary["problems"], [])
        before = self.memory.snapshot()
        calls = len(runner.calls)
        outcome = self.feedback()
        self.assertEqual(outcome["fit_policy_id"], FIT_POLICY_ID)
        self.assertEqual(outcome["resolved_count"], 2)
        self.assertEqual(outcome["pending_count"], 0)
        self.assertEqual(outcome["fit_pending_count"], 0)
        self.assertTrue(outcome["reviewed"])
        self.assertTrue(outcome["unseen_promising"])
        self.assertTrue(outcome["useful_progress"])
        events = len(list((self.memory.root / "events").glob("*.json")))
        self.assertEqual(self.feedback(), outcome)
        self.assertEqual(len(list((self.memory.root / "events").glob("*.json"))), events)
        self.assertEqual(len(runner.calls), calls)
        self.assertEqual(self.memory.snapshot()["captures"], before["captures"])
        self.assertEqual(self.memory.snapshot()["assessments"], before["assessments"])

    def test_four_matches_with_unknowns_are_pending_not_zero_query_yield(self):
        self.committed_capture()
        run_flow(self.memory, CurrentFitRunner(4), as_of=NOW, fit_policy_id=FIT_POLICY_ID)
        outcome = self.feedback()
        self.assertEqual(outcome["resolved_count"], 2)
        self.assertEqual(outcome["pending_count"], 0)
        self.assertEqual(outcome["fit_pending_count"], 2)
        self.assertFalse(outcome["reviewed"])
        self.assertIsNone(outcome["unseen_promising"])
        self.assertIsNone(outcome["useful_progress"])

    def test_supported_negatives_can_finish_an_unpromising_query(self):
        self.committed_capture()
        run_flow(self.memory, CurrentFitRunner(4, negative=True), as_of=NOW,
                 fit_policy_id=FIT_POLICY_ID)
        outcome = self.feedback()
        self.assertEqual(outcome["fit_pending_count"], 0)
        self.assertTrue(outcome["reviewed"])
        self.assertFalse(outcome["unseen_promising"])
        self.assertFalse(outcome["useful_progress"])

    def test_legacy_feedback_cannot_remain_current_ready_after_policy_switch(self):
        self.committed_capture()
        run_flow(self.memory, FakeRunner(), as_of=NOW, fit_policy_id=LEGACY_POLICY_ID)
        historical_policy = policy_for_fit(LEGACY_POLICY_ID)
        with patch("product_radar.service.load_policy", return_value=historical_policy):
            historical = self.feedback()
        self.assertEqual(historical["fit_policy_id"], LEGACY_POLICY_ID)
        self.assertTrue(historical["reviewed"])
        before = self.memory.snapshot()
        current = self.feedback()
        self.assertEqual(current["fit_policy_id"], FIT_POLICY_ID)
        self.assertFalse(current["reviewed"])
        self.assertIsNone(current["unseen_promising"])
        self.assertIsNone(current["useful_progress"])
        self.assertEqual(self.memory.snapshot()["assessments"], before["assessments"])
        self.assertEqual(self.memory.snapshot()["captures"], before["captures"])
        with self.financial():
            reused = find_existing_task(self.memory, self.action, as_of=NOW)
        self.assertEqual(reused["reuse_kind"], "fresh_capture")
        self.assertEqual(reused["attempt_id"], "fixture-attempt")


if __name__ == "__main__":
    unittest.main()
