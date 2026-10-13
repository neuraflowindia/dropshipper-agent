"""Finite product-specific validation uses isolated saved/provider fixtures."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.models import SafetyError, digest
from product_radar.fit_policy import ACTIVE_CRITERION_IDS, FIT_POLICY_ID, LEGACY_POLICY_ID
from product_radar.research_memory import change, key
from product_radar.research_operations import plan_meta_validation, bind_prepared_action
from product_radar.service import policy_for_fit
import test_research_batch as batch_fixture
from test_report_selection import fixture, NOW


class MetaValidationTests(unittest.TestCase):
    prepare = batch_fixture.ResearchBatchTests.prepare
    execute = batch_fixture.ResearchBatchTests.execute

    def setUp(self):
        batch_fixture.ResearchBatchTests.setUp(self)
        # These saved thirteen-row fixtures exercise their reviewed historical
        # policy. Production's current default must not reinterpret them.
        policy = policy_for_fit(LEGACY_POLICY_ID)
        policy_patch = patch("product_radar.service.load_policy", return_value=policy)
        policy_patch.start()
        self.addCleanup(policy_patch.stop)

    def seed(self, count=1, *, sink=False, mutate=None):
        state = fixture(count).state
        for n in range(count):
            cid, sid, oid = f"c{n:03d}", f"semantic{n:03d}", f"meta{n:03d}"
            original = state["observations"][oid]
            original["source"] = "amazon"
            original["source_item"] = f"origin-ASIN{n}"
            original["facts"] = deepcopy(state["observations"][f"amazon{n:03d}"]["facts"])
            if sink and n == 0:
                description = state["concepts"][cid]["description"]
                description.update(label="Sink caddy", function="Store sink essentials", mechanism="manual",
                                   audience="adult", features=["draining"])
                original["semantic"]["title"] = description["label"]
                answer = state["descriptions"][sid]["result"]
                answer["products"] = [deepcopy(description)]
                old_child = next(i for i, a in state["associations"].items() if a["concept_id"] == cid)
                state["associations"][key("child", [sid, 0, digest(answer)])] = state["associations"].pop(old_child)
                item = {"id": cid, "description": description, "observations": [{"id": oid, "semantic_id": sid,
                                                                                  "source_text": original["semantic"]}]}
                state["assessments"][cid]["input_hash"] = digest(item)
                state["market_reviews"]["review0"]["description_hash"] = digest(description)
                supplier = state["supplier_assessments"]["supplier-record0"]
                supplier["input"]["product"] = dict(description, concept_id=cid)
                supplier["input_hash"] = digest(supplier["input"])
        if mutate:
            mutate(state)
        for observation in state["observations"].values():
            observation["capture_id"] = key("capture", [observation["source"], observation["acquisition_id"]])
        self.memory.commit("seed-validated-amazon", [change(table, rid, row)
                           for table, rows in state.items() for rid, row in rows.items()])

    def plan_validation(self, **options):
        self.plan = plan_meta_validation(self.memory, as_of=NOW, **options)
        return self.plan

    def test_accepted_labels_and_ten_ad_request_without_launch(self):
        self.seed(4)
        plan = self.plan_validation()
        self.assertEqual(len(plan["actions"]), 3)
        self.assertEqual(plan["budget"]["discovery_estimate_usd"], "0.15")
        for action in plan["actions"]:
            self.assertEqual(action["request"]["purpose"], "validation")
            self.assertEqual(action["request"]["coverage"], {"limit": 10})
            self.assertEqual(action["phrase"], action["target"]["label"])
            self.assertIn(action["target"]["concept_id"], action["baseline_concepts"])
        self.assertEqual(self.launches, [])
        self.assertEqual(self.runner.calls, [])
        self.assertIn("Product-specific Meta checks", (self.memory.root / "next_searches.html").read_text())

    def test_limit_and_money_envelopes_do_not_create_funds(self):
        self.seed(6)
        self.assertEqual(len(self.plan_validation(max_actions=5)["actions"]), 5)
        self.assertEqual(len(self.plan_validation(budget_usd="0.09")["actions"]), 1)
        self.assertEqual(len(self.plan_validation(budget_usd="0.00")["actions"]), 0)
        for value in (0, 6, True):
            with self.assertRaisesRegex(SafetyError, "VALIDATION_PLAN_LIMIT_INVALID"):
                self.plan_validation(max_actions=value)

    def test_default_validation_uses_compact_profile_but_named_refinement_stays_authoritative(self):
        self.seed()
        with patch("product_radar.product_queries.product_query_terms", return_value=[
                {"source": "meta", "text": "doorway pull-up bar", "scope": "subtype"}]) as profiles:
            default = self.plan_validation()
            profiles.assert_called_once_with(self.memory, "c000", source="meta", limit=1)
            refined = self.plan_validation(concept_id="c000", term="telescopic pull-up bar",
                                            reason="Check the relevant adjustable subtype.")
            self.assertEqual(profiles.call_count, 1)
        self.assertEqual(default["actions"][0]["phrase"], "doorway pull-up bar")
        self.assertEqual(refined["actions"][0]["phrase"], "telescopic pull-up bar")
        self.assertEqual(default["actions"][0]["target"]["description_hash"],
                         refined["actions"][0]["target"]["description_hash"])
        self.assertEqual(self.launches, [])
        self.assertEqual(self.runner.calls, [])

    def test_threshold_brand_published_and_identity_filters(self):
        def mutate(state):
            state["assessments"]["c000"]["result"]["criteria"][7]["state"] = "Unclear"
            state["assessments"]["c000"]["result"]["criteria"][8]["state"] = "Unclear"
            state["concepts"]["c001"]["description"]["brand"] = "established"
            state["published_history"]["history"] = {"concept_id": "c002", "status": "published", "client_id": "default"}
            state["concepts"]["c003"]["identity_conflicted"] = True
        self.seed(5, mutate=mutate)
        self.assertEqual([a["target"]["concept_id"] for a in self.plan_validation()["actions"]], ["c004"])

    def test_exact_plan_idempotence_and_unrelated_supplier_or_clock_not_question(self):
        self.seed()
        first = self.plan_validation()
        events = len(list((self.memory.root / "events").glob("*.json")))
        self.assertEqual(self.plan_validation(), first)
        self.assertEqual(events, len(list((self.memory.root / "events").glob("*.json"))))
        old = self.memory.snapshot()["supplier_assessments"]["supplier-record0"]
        self.memory.put("supplier_assessments", "supplier-record0", dict(old, checked_at="2026-10-06T10:01:00+00:00"), previous=old)
        newer = plan_meta_validation(self.memory, as_of="2026-10-06T11:00:00+00:00")
        self.assertEqual(first["actions"][0]["target"]["question_fingerprint"], newer["actions"][0]["target"]["question_fingerprint"])

    def test_unusable_label_is_a_named_deferral_not_truncation(self):
        self.seed(mutate=lambda s: s["concepts"]["c000"]["description"].update(label="x" * 121))
        # A changed description first needs E refresh; deliberately update its
        # accepted input to exercise the independent query-length limitation.
        self.reaccept_description()
        plan = self.plan_validation()
        self.assertEqual(plan["actions"], [])
        self.assertIn("120 characters", plan["decisions"][0]["reason"])

    def reaccept_description(self):
        state = self.memory.snapshot(); cid = "c000"
        old = state["assessments"][cid]
        item = {"id": cid, "description": state["concepts"][cid]["description"], "observations": [
            {"id": "meta000", "semantic_id": "semantic000", "source_text": state["observations"]["meta000"]["semantic"]}]}
        self.memory.put("assessments", cid, dict(old, input_hash=digest(item)), previous=old)
        old_review = state["market_reviews"]["review0"]
        self.memory.put("market_reviews", "review0", dict(old_review, description_hash=digest(item["description"])), previous=old_review)

    def test_changed_target_defers_new_launch_but_other_action_runs(self):
        self.seed(2)
        plan = self.plan_validation()
        target_id = plan["actions"][0]["target"]["concept_id"]
        old = self.memory.snapshot()["concepts"][target_id]
        self.memory.put("concepts", target_id, dict(old, description=dict(old["description"], mechanism="materially changed")), previous=old)
        result = self.execute()
        self.assertEqual(result["actions"][0]["status"], "deferred")
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(result["actions"][1]["status"], "captured")

    def test_submitted_unknown_target_change_resumes_existing_attempt(self):
        self.seed()
        self.plan_validation()
        action = self.plan["actions"][0]
        prepared = self.prepare(self.plan, action)
        self.behaviors[prepared.attempt_id] = "unknown"
        first = self.execute(action_limit=1)
        self.assertEqual(first["actions"][0]["status"], "blocked")
        old = self.memory.snapshot()["concepts"]["c000"]
        self.memory.put("concepts", "c000", dict(old, description=dict(old["description"], mechanism="changed")), previous=old)
        self.behaviors[prepared.attempt_id] = "success"
        second = self.execute(action_limit=1)
        self.assertEqual(len(self.launches), 1)
        self.assertIn((prepared.attempt_id, True), self.runs)
        self.assertEqual(second["actions"][0]["status"], "captured")
        self.assertFalse(second["actions"][0]["target_result"]["useful_progress"])

    def test_irrelevant_results_do_not_attach_to_query_target(self):
        self.seed()
        self.plan_validation()
        result = self.execute(action_limit=1)
        target = result["actions"][0]["target_result"]
        self.assertEqual(target["status"], "coverage_still_insufficient")
        self.assertFalse(target["useful_progress"])
        state = self.memory.snapshot()
        new_associations = [r for r in state["associations"].values() if r["semantic_id"] != "semantic000"]
        self.assertTrue(new_associations)
        self.assertFalse(any(r["concept_id"] == "c000" for r in new_associations))
        self.assertEqual(state["query_tasks"][result["actions"][0]["task_id"]]["target"], self.plan["actions"][0]["target"])

    def test_relevant_target_progress_not_new_product_and_replay_reuses(self):
        self.seed(sink=True)
        self.plan_validation()
        first = self.execute(action_limit=1)
        self.assertEqual(first["actions"][0]["target_result"]["status"], "coverage_satisfied")
        self.assertTrue(first["actions"][0]["target_result"]["useful_progress"])
        outcome = next(iter(self.memory.snapshot()["query_outcomes"].values()))
        self.assertEqual(outcome["known_count"], 1)
        self.assertFalse(outcome["unseen_promising"])
        self.assertTrue(outcome["useful_progress"])
        calls = len(self.runner.calls)
        second = self.execute(action_limit=1)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(len(self.runner.calls), calls)
        self.assertTrue(second["replay"])

    def test_completed_unhelpful_unchanged_question_does_not_recollect(self):
        self.seed()
        self.plan_validation()
        first = self.execute(action_limit=1)
        # Simulate unavailable cache: original result/history must prevent a
        # new automatic collection despite the still-missing source.
        with patch("product_radar.query_dispatch.find_existing_task", return_value=None):
            plan = plan_meta_validation(self.memory, as_of=NOW)
        self.assertEqual(plan["actions"], [])
        self.assertIn("unchanged question", plan["decisions"][0]["reason"])
        self.assertEqual(len(self.launches), 1)

    def test_pending_interpretation_has_unknown_not_negative_target_result(self):
        self.seed()
        self.plan_validation()
        self.runner = None
        result = self.execute(action_limit=1)
        self.assertEqual(result["actions"][0]["target_result"]["status"], "interpretation_pending")
        self.assertIsNone(result["actions"][0]["target_result"]["useful_progress"])

    def test_optional_source_failure_preserves_independent_validation(self):
        self.seed(2)
        self.plan_validation()
        first = self.prepare(self.plan, self.plan["actions"][0])
        self.behaviors[first.attempt_id] = "failed"
        result = self.execute(action_limit=2)
        self.assertEqual(len(self.launches), 2)
        self.assertEqual(result["actions"][0]["target_result"]["status"], "source_unavailable")
        self.assertEqual(result["actions"][1]["status"], "captured")

    def test_fresh_completed_exact_task_reused_across_plans(self):
        self.seed()
        self.plan_validation()
        first = self.execute(action_limit=1)
        new_plan = plan_meta_validation(self.memory, as_of="2026-10-06T10:01:00+00:00")
        self.assertEqual(new_plan["actions"][0]["kind"], "reuse")
        self.plan = new_plan
        second = self.execute(action_limit=1)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(second["actions"][0]["task_id"], first["actions"][0]["task_id"])
        self.assertEqual(second["actions"][0]["target"], new_plan["actions"][0]["target"])

    def test_prepared_unfunded_task_still_needs_money_envelope(self):
        self.seed(); self.plan_validation()
        prepared = self.prepare(self.plan, self.plan["actions"][0])
        bind_prepared_action(self.memory, self.plan["id"], self.plan["actions"][0]["action_id"], prepared)
        self.assertEqual(plan_meta_validation(self.memory, as_of=NOW, budget_usd="0")["actions"], [])
        action = plan_meta_validation(self.memory, as_of=NOW, budget_usd="0.05")["actions"][0]
        self.assertEqual(action["cost_estimate_usd"], "0.05")

    def test_duplicate_exact_queries_collapsed_without_merging_concepts(self):
        self.seed(2)
        old = self.memory.snapshot()["concepts"]["c001"]
        self.memory.put("concepts", "c001", dict(old, description=dict(old["description"], label="Synthetic product 0")), previous=old)
        state = self.memory.snapshot(); old = state["assessments"]["c001"]
        item = {"id": "c001", "description": state["concepts"]["c001"]["description"], "observations": [
            {"id": "meta001", "semantic_id": "semantic001", "source_text": state["observations"]["meta001"]["semantic"]}]}
        self.memory.put("assessments", "c001", dict(old, input_hash=digest(item)), previous=old)
        old = state["market_reviews"]["review1"]
        self.memory.put("market_reviews", "review1", dict(old, description_hash=digest(item["description"])), previous=old)
        plan = self.plan_validation()
        self.assertEqual(len(plan["actions"]), 1)
        self.assertEqual(len(self.memory.snapshot()["concepts"]), 2)
        self.assertTrue(any(d["decision"] == "omit" and "exact query" in d["reason"] for d in plan["decisions"]))

    def test_accepted_market_dependency_change_changes_question(self):
        self.seed(); first = self.plan_validation()["actions"][0]["target"]["question_fingerprint"]
        old = self.memory.snapshot()["market_reviews"]["review0"]
        relevance = [dict(old["relevance"][0], relationship="relevant_family", reason="Reviewed family distinction.")]
        self.memory.put("market_reviews", "review0", dict(old, relevance=relevance), previous=old)
        second = self.plan_validation()["actions"][0]["target"]["question_fingerprint"]
        self.assertNotEqual(first, second)

    def test_partial_target_evidence_does_not_turn_its_new_fingerprint_into_retry(self):
        self.seed(sink=True); self.plan_validation()
        # The product matches, but advertiser/activity/date omissions mean its
        # newly attached evidence cannot establish assessable Meta coverage.
        incomplete = [{"ad_archive_id": "999", "snapshot": {"title": "Blue sink caddy"}}]
        with patch("test_research_batch.rows", return_value=incomplete):
            result = self.execute(action_limit=1)
        target_result = result["actions"][0]["target_result"]
        self.assertEqual(target_result["status"], "coverage_still_insufficient")
        self.assertFalse(target_result["useful_progress"])
        self.assertIn("question_fingerprint", target_result)
        self.assertNotEqual(target_result["question_fingerprint"], self.plan["actions"][0]["target"]["question_fingerprint"])
        with patch("product_radar.query_dispatch.find_existing_task", return_value=None):
            new_plan = plan_meta_validation(self.memory, as_of=NOW)
        self.assertEqual(new_plan["actions"], [])
        self.assertIn("unchanged question", new_plan["decisions"][0]["reason"])

    def test_named_refinement_after_empty_full_label_preserves_evidence_question(self):
        self.seed(2); original = self.plan_validation()
        target = original["actions"][0]["target"]
        with patch("test_research_batch.rows", return_value=[]):
            result = self.execute(action_limit=1)
        self.assertEqual(result["actions"][0]["target_result"]["status"], "coverage_still_insufficient")
        refined = self.plan_validation(concept_id=target["concept_id"], term="  bathroom   organiser  ",
                                       reason="The full shop label returned no ads; try its ordinary product wording.")
        self.assertEqual(len(refined["actions"]), 1)
        action = refined["actions"][0]
        self.assertEqual(action["phrase"], "bathroom organiser")
        self.assertEqual(action["target"], target)
        self.assertEqual(action["refinement"], refined["refinement"])
        self.assertEqual(action["refinement_hash"], digest(refined["refinement"]))
        self.assertNotEqual(action["semantic_key"], original["actions"][0]["semantic_key"])
        self.assertEqual(action["cost_estimate_usd"], "0.05")
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(len(self.memory.snapshot()["concepts"]), 2)
        self.assertIn("full shop label", (self.memory.root / "next_searches.html").read_text())

    def test_named_refinement_replays_exact_task_without_a_second_launch(self):
        self.seed()
        options = {"concept_id": "c000", "term": "bathroom organiser", "reason": "Try ordinary wording."}
        first_plan = self.plan_validation(**options)
        self.assertEqual(self.plan_validation(**options), first_plan)
        first = self.execute(action_limit=1)
        # These fixture ads describe a different product. The query cannot
        # attach them to the named target, or claim positive target coverage.
        self.assertFalse(first["actions"][0]["target_result"]["useful_progress"])
        self.assertEqual(first["actions"][0]["target_result"]["status"], "coverage_still_insufficient")
        revised = dict(options, reason="A different explanation for the same ordinary wording.")
        new_plan = self.plan_validation(**revised)
        self.assertEqual(new_plan["actions"][0]["kind"], "reuse")
        self.assertEqual(new_plan["actions"][0]["cost_estimate_usd"], "0.00")
        second = self.execute(action_limit=1)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(second["actions"][0]["task_id"], first["actions"][0]["task_id"])

    def test_recorded_refinement_without_cache_is_not_automatic_retry(self):
        self.seed()
        options = {"concept_id": "c000", "term": "bathroom organiser", "reason": "Try ordinary wording."}
        self.plan_validation(**options); self.execute(action_limit=1)
        with patch("product_radar.query_dispatch.find_existing_task", return_value=None):
            repeated = self.plan_validation(**dict(options, reason="Another explanation, not another query."))
            different = self.plan_validation(**dict(options, term="shower organiser"))
            normal = self.plan_validation()
        self.assertEqual(repeated["actions"], [])
        self.assertEqual(normal["actions"], [])
        self.assertEqual(len(different["actions"]), 1)
        self.assertEqual(different["actions"][0]["phrase"], "shower organiser")
        self.assertEqual(len(self.launches), 1)

    def test_refinement_needs_complete_valid_public_wording_and_reason(self):
        self.seed()
        invalid = ({"concept_id": "c000"}, {"term": "organiser"}, {"reason": "Try ordinary wording."},
                   {"concept_id": "c000", "term": "organiser"},
                   {"concept_id": "c000", "term": "", "reason": "Try ordinary wording."},
                   {"concept_id": "c000", "term": "x" * 121, "reason": "Try ordinary wording."},
                   {"concept_id": "c000", "term": "organiser\nnew", "reason": "Try ordinary wording."},
                   {"concept_id": "c000", "term": 1, "reason": "Try ordinary wording."},
                   {"concept_id": "c000", "term": "organiser", "reason": " "},
                   {"concept_id": "c000", "term": "organiser", "reason": "x" * 1001},
                   {"concept_id": "c000", "term": "organiser", "reason": False})
        for options in invalid:
            with self.subTest(options=options), self.assertRaisesRegex(SafetyError, "VALIDATION_REFINEMENT_INVALID"):
                self.plan_validation(**options)
        for concept_id in ("missing", "C000"):
            with self.assertRaisesRegex(SafetyError, "VALIDATION_REFINEMENT_TARGET_UNAVAILABLE"):
                self.plan_validation(concept_id=concept_id, term="organiser", reason="Try ordinary wording.")
        self.assertEqual(self.launches, [])

    def test_refinement_cannot_bypass_changed_or_excluded_target(self):
        self.seed(2)
        old = self.memory.snapshot()["concepts"]["c000"]
        self.memory.put("concepts", "c000", dict(old, description=dict(old["description"], brand="established")), previous=old)
        old = self.memory.snapshot()["concepts"]["c001"]
        self.memory.put("concepts", "c001", dict(old, description=dict(old["description"], mechanism="changed")), previous=old)
        for concept_id in ("c000", "c001"):
            with self.assertRaisesRegex(SafetyError, "VALIDATION_REFINEMENT_TARGET_UNAVAILABLE"):
                self.plan_validation(concept_id=concept_id, term="organiser", reason="Try ordinary wording.")
        self.assertEqual(self.launches, [])

    def test_refinement_money_and_unfunded_preparation_still_apply(self):
        self.seed(2)
        options = {"concept_id": "c000", "term": "bathroom organiser", "reason": "Try ordinary wording."}
        self.assertEqual(self.plan_validation(**options, budget_usd="0.049")["actions"], [])
        plan = self.plan_validation(**options, max_actions=1, budget_usd="0.05")
        self.assertEqual(len(plan["actions"]), 1)
        prepared = self.prepare(plan, plan["actions"][0])
        bind_prepared_action(self.memory, plan["id"], plan["actions"][0]["action_id"], prepared)
        self.assertEqual(self.plan_validation(**options, budget_usd="0")["actions"], [])
        self.assertEqual(self.plan_validation(**options, budget_usd="0.05")["actions"][0]["cost_estimate_usd"], "0.05")

    def test_refinement_named_as_original_query_reuses_original_capture(self):
        self.seed(); original = self.plan_validation()
        first = self.execute(action_limit=1)
        new_plan = self.plan_validation(concept_id="c000", term=original["actions"][0]["phrase"],
                                       reason="Explicitly inspect the saved full-label answer.")
        self.assertEqual(new_plan["actions"][0]["kind"], "reuse")
        second = self.execute(action_limit=1)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(second["actions"][0]["task_id"], first["actions"][0]["task_id"])

    def test_funded_uncertain_refinement_recovers_without_new_money_or_submission(self):
        self.seed()
        options = {"concept_id": "c000", "term": "bathroom organiser", "reason": "Try ordinary wording."}
        plan = self.plan_validation(**options)
        prepared = self.prepare(plan, plan["actions"][0])
        self.behaviors[prepared.attempt_id] = "unknown"
        first = self.execute(action_limit=1)
        self.assertEqual(first["actions"][0]["status"], "blocked")
        recovery = self.plan_validation(**options, budget_usd="0")
        self.assertEqual(len(recovery["actions"]), 1)
        self.assertEqual(recovery["actions"][0]["cost_estimate_usd"], "0.00")
        self.behaviors[prepared.attempt_id] = "success"
        self.execute(action_limit=1)
        self.assertEqual(len(self.launches), 1)
        self.assertIn((prepared.attempt_id, True), self.runs)

    def test_refinement_still_rechecks_target_before_new_launch(self):
        self.seed()
        self.plan_validation(concept_id="c000", term="bathroom organiser", reason="Try ordinary wording.")
        old = self.memory.snapshot()["concepts"]["c000"]
        self.memory.put("concepts", "c000", dict(old, description=dict(old["description"], mechanism="changed")), previous=old)
        result = self.execute(action_limit=1)
        self.assertEqual(result["actions"][0]["status"], "deferred")
        self.assertEqual(self.launches, [])

    def test_different_targets_reuse_one_query_but_measure_separate_market_results(self):
        self.seed(2, sink=True)
        self.plan_validation(concept_id="c000", term="bathroom organiser", reason="Check the sink product.")
        first = self.execute(action_limit=1)
        self.assertEqual(first["actions"][0]["target_result"]["status"], "coverage_satisfied")
        self.assertTrue(first["actions"][0]["target_result"]["useful_progress"])
        before = self.memory.snapshot()
        task_id = first["actions"][0]["task_id"]
        original_task = deepcopy(before["query_tasks"][task_id])
        with self.memory.journal.locked():
            original_finance = deepcopy(self.memory.journal.state())
        self.plan_validation(concept_id="c001", term="bathroom organiser", reason="Check a different product with the same public query.")
        self.assertEqual(self.plan["actions"][0]["kind"], "reuse")
        self.assertEqual(self.plan["actions"][0]["cost_estimate_usd"], "0.00")
        second = self.execute(action_limit=1)
        entry = second["actions"][0]
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(entry["task_id"], task_id)
        self.assertEqual(entry["target"]["concept_id"], "c001")
        self.assertNotEqual(entry["family_id"], original_task["family_id"])
        self.assertEqual(entry["target_result"]["status"], "coverage_still_insufficient")
        self.assertFalse(entry["target_result"]["useful_progress"])
        self.assertEqual(self.memory.snapshot()["query_tasks"][task_id], original_task)
        self.assertEqual(self.memory.snapshot()["captures"], before["captures"])
        self.assertEqual(len(self.memory.snapshot()["query_tasks"]), 1)
        self.assertEqual(len(self.memory.snapshot()["query_outcomes"]), 1)
        with self.memory.journal.locked():
            self.assertEqual(self.memory.journal.state(), original_finance)

    def test_cross_target_reuse_keeps_shared_pending_interpretation_unknown(self):
        self.seed(2)
        self.plan_validation(concept_id="c000", term="bathroom organiser", reason="Check the first product.")
        self.runner = None
        first = self.execute(action_limit=1)
        self.assertEqual(first["actions"][0]["target_result"]["status"], "interpretation_pending")
        self.plan_validation(concept_id="c001", term="bathroom organiser", reason="Check another product using saved ads.")
        second = self.execute(action_limit=1)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(second["actions"][0]["target_result"]["status"], "interpretation_pending")
        self.assertIsNone(second["actions"][0]["target_result"]["useful_progress"])

    def test_cross_target_uncertain_work_recovers_exact_original_attempt(self):
        self.seed(2, sink=True)
        plan = self.plan_validation(concept_id="c000", term="bathroom organiser", reason="Check the first product.")
        prepared = self.prepare(plan, plan["actions"][0])
        self.behaviors[prepared.attempt_id] = "unknown"
        first = self.execute(action_limit=1)
        self.assertEqual(first["actions"][0]["status"], "blocked")
        plan = self.plan_validation(concept_id="c001", term="bathroom organiser", reason="Check another product using the same collection.", budget_usd="0")
        self.assertEqual(plan["actions"][0]["cost_estimate_usd"], "0.00")
        self.behaviors[prepared.attempt_id] = "success"
        second = self.execute(action_limit=1)
        self.assertEqual(len(self.launches), 1)
        self.assertIn((prepared.attempt_id, True), self.runs)
        self.assertEqual(second["actions"][0]["target"]["concept_id"], "c001")
        self.assertEqual(second["actions"][0]["target_result"]["status"], "coverage_still_insufficient")
        self.assertFalse(second["actions"][0]["target_result"]["useful_progress"])

    def test_cross_target_unsubmitted_preparation_reuses_task_but_requires_money(self):
        self.seed(2)
        plan = self.plan_validation(concept_id="c000", term="bathroom organiser", reason="Check the first product.")
        prepared = self.prepare(plan, plan["actions"][0])
        bound = bind_prepared_action(self.memory, plan["id"], plan["actions"][0]["action_id"], prepared)
        options = {"concept_id": "c001", "term": "bathroom organiser", "reason": "Check another product using the same collection."}
        self.assertEqual(self.plan_validation(**options, budget_usd="0")["actions"], [])
        self.plan_validation(**options, budget_usd="0.05")
        second = self.execute(action_limit=1)
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(second["actions"][0]["task_id"], bound["id"])
        self.assertEqual(second["actions"][0]["target"]["concept_id"], "c001")
        self.assertEqual(self.memory.snapshot()["query_tasks"][bound["id"]]["target"]["concept_id"], "c000")
        self.assertEqual(len(self.memory.snapshot()["query_tasks"]), 1)


class CurrentMetaValidationTests(unittest.TestCase):
    """The current policy has separate fixtures and keeps both passing tiers."""

    prepare = MetaValidationTests.prepare
    execute = MetaValidationTests.execute
    seed = MetaValidationTests.seed
    plan_validation = MetaValidationTests.plan_validation

    def setUp(self):
        MetaValidationTests.setUp(self)
        policy = policy_for_fit(FIT_POLICY_ID)
        policy_patch = patch("product_radar.service.load_policy", return_value=policy)
        policy_patch.start()
        self.addCleanup(policy_patch.stop)

    def seed_current(self, matches, *, negative=False):
        def current_assessments(state):
            for n, match_count in enumerate(matches):
                assessment = state["assessments"][f"c{n:03d}"]
                criteria = [row for row in assessment["result"]["criteria"]
                            if row["id"] in ACTIVE_CRITERION_IDS]
                for index, row in enumerate(criteria):
                    row["state"] = ("Meets" if index < match_count else
                                    "Does not meet" if negative else "Unclear")
                assessment["result"]["criteria"] = criteria
                assessment["result"]["audience_positioning"] = {
                    "positioning": "unclear", "reason": "Synthetic buyer context has not been assessed."}
                assessment.update(fit_policy_id=FIT_POLICY_ID,
                                  contract_version="research-e-fit10-v1")
        self.seed(len(matches), mutate=current_assessments)

    def test_good_and_decent_both_get_market_checks_with_good_first(self):
        # The Decent product has the smaller ID. Fit priority, not incidental
        # saved order, must decide which product gets the first limited check.
        self.seed_current((5, 6, 4))
        before = self.memory.snapshot()
        plan = self.plan_validation(max_actions=2)
        self.assertEqual([row["target"]["concept_id"] for row in plan["actions"]],
                         ["c001", "c000"])
        self.assertEqual(plan["budget"]["discovery_estimate_usd"], "0.10")
        after = self.memory.snapshot()
        self.assertEqual(after["observations"], before["observations"])
        self.assertEqual(after["assessments"], before["assessments"])
        self.assertEqual(self.launches, [])
        self.assertEqual(self.runner.calls, [])

    def test_five_matches_qualify_but_four_unknowns_remain_pending(self):
        self.seed_current((5, 4))
        from product_radar.research_operations import validation_targets
        targets, pool = validation_targets(self.memory, as_of=NOW)
        self.assertEqual(set(targets), {"c000"})
        pending = next(row for row in pool["decisions"] if row["concept_id"] == "c001")
        self.assertEqual(pending["decision"], "deferred")
        self.assertIn("fit_information_pending", pending["codes"])
        self.assertNotIn("below_checklist_threshold", pending["codes"])
        self.assertEqual([row["target"]["concept_id"]
                          for row in self.plan_validation()["actions"]], ["c000"])

    def test_supported_negatives_below_five_do_not_get_optional_searches(self):
        self.seed_current((5, 4), negative=True)
        from product_radar.research_operations import validation_targets
        targets, pool = validation_targets(self.memory, as_of=NOW)
        self.assertEqual(set(targets), {"c000"})
        below = next(row for row in pool["decisions"] if row["concept_id"] == "c001")
        self.assertEqual(below["decision"], "excluded")
        self.assertIn("below_checklist_threshold", below["codes"])
        self.assertEqual([row["target"]["concept_id"]
                          for row in self.plan_validation()["actions"]], ["c000"])

    def test_historical_checklist_needs_current_projection_not_recollection(self):
        self.seed()
        before = self.memory.snapshot()
        from product_radar.research_operations import validation_targets
        targets, pool = validation_targets(self.memory, as_of=NOW)
        self.assertEqual(targets, {})
        self.assertIn("checklist_review_required", pool["decisions"][0]["codes"])
        self.assertEqual(self.plan_validation()["actions"], [])
        after = self.memory.snapshot()
        self.assertEqual(after["observations"], before["observations"])
        self.assertEqual(after["assessments"], before["assessments"])
        self.assertEqual(self.launches, [])


if __name__ == "__main__":
    unittest.main()
