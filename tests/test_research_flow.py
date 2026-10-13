"""Deterministic fixtures test wiring, not actual model judgment or demand."""

from copy import deepcopy
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.models import SafetyError, canonical, digest
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.providers.apify import map_meta_rows
from product_radar.research_memory import ResearchMemory, change, import_capture, import_saved_attempt, key
from product_radar.research_ai import Jobs, CRITERIA, validate_results, parse_codex_output, prepare_retry
from product_radar.research_flow import CHECKLIST_SCOPE, candidate_summary, exclude_established_brand, run_flow, write_summary, research_status, market_follow_up, problem
from product_radar.discovery import compute_market_signals, find_possible_matches
from product_radar.work_turns import DEFAULT_POLICY, bind_work_turn, open_work_turn

NOW = "2026-10-05T10:00:00+00:00"
OBSERVED = "2026-10-04T10:00:00+00:00"


def rows():
    return [{"ad_archive_id": str(100 + n), "page_id": str(200 + n), "is_active": True,
             "start_date": 1788220800, "snapshot": {"title": title}}
            for n, title in enumerate(("Blue sink caddy", "Pink sink caddy", "Powered cleaning brush", "Unclear offer"))]


def capture(memory, values=None, acquisition="fixture1", observed=OBSERVED):
    values = rows() if values is None else values
    return import_capture(memory, source="meta", acquisition_id=acquisition, observed_at=observed,
                          mapped=map_meta_rows(values, purpose="discovery", query_reference="fixture"),
                          capture_hash=digest(values), reference={"fixture": "synthetic"})


class FakeRunner:
    identity = "synthetic-only-v1"

    def __init__(self):
        self.calls = []

    def __call__(self, role, payload, folder):
        self.calls.append((role, deepcopy(payload)))
        results = []
        for item in payload["items"]:
            if role == "A":
                title = item["source_text"].get("title", "")
                product = {"label": "Sink caddy" if "caddy" in title else "Powered cleaning brush",
                           "category": "Home and Kitchen", "function": "Store sink essentials" if "caddy" in title else "Clean surfaces",
                           "mechanism": "manual" if "caddy" in title else "powered", "audience": "adult",
                           "features": ["draining" if "caddy" in title else "rotating brush"], "basis": "observed",
                           "reason": "Fictional description for a wiring test.", "brand": "generic_or_small"}
                products = [] if title == "Unclear offer" else [product]
                results.append({"id": item["id"], "kind": "product" if products else "uninterpretable", "products": products,
                                "reason": "Test fixture, not a researched recommendation."})
            elif role == "B":
                same = next((c for c in item["candidates"] if c["description"]["label"] == item["description"]["label"]), None)
                results.append({"id": item["id"], "decision": "same" if same else "different",
                                "match_id": same["id"] if same else None, "reason": "Compare supplied function, not seller or colour."})
            else:
                refs = [o["id"] for o in item["observations"]]
                results.append({"id": item["id"], "criteria": [
                    {"id": n, "state": "Meets" if n != 3 and n <= 9 else "Unclear", "basis": "inferred",
                     "references": refs, "reason": "Synthetic judgment for testing arithmetic, not a real benefit claim."}
                    for n in range(1, 14)],
                    "relevance": [{"id": r, "relationship": "exact_product", "reason": "Fixture association."} for r in refs],
                    "compliance_note": "Client review where relevant.", "missing_questions": ["Supplier capability has not been checked."]})
        return {"results": results}, {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 10}


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = Journal(self.root / "finance")
        self.memory = ResearchMemory(self.root / "research", self.journal, evidence_type="synthetic")
        self.runner = FakeRunner()

    def spent_turn(self):
        policy = deepcopy(DEFAULT_POLICY)
        policy.update(model_executions=1, report_slots=0)
        turn = open_work_turn(self.memory, "flow-preparation-limit", at=NOW, policy=policy)
        with bind_work_turn(self.memory, turn["id"]):
            Jobs(self.memory, self.runner).run("A", {"items": [
                {"id": "spent-slot", "source_text": {"title": "Unclear offer"}}]})
        self.runner.calls.clear()
        return turn

    def test_spent_turn_skips_fresh_descriptions_without_per_item_problem_writes(self):
        capture(self.memory)
        turn = self.spent_turn()
        before = self.memory.snapshot_with_provenance()
        with bind_work_turn(self.memory, turn["id"]), \
                patch("product_radar.model_recovery.run_current_model_job",
                      side_effect=AssertionError("Fresh work must wait before entering Jobs")):
            result = run_flow(self.memory, self.runner, as_of=NOW)
        self.assertEqual(result["preparation_waits"], [
            {"stage": "description", "reason": "WORK_TURN_MODEL_LIMIT", "item_count": 4}])
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        self.assertEqual(self.runner.calls, [])

    def test_spent_turn_recovers_original_descriptions_and_continues_local_work(self):
        capture(self.memory)
        put = self.memory.put
        def interrupted(table, *args, **kwargs):
            if table == "descriptions":
                raise RuntimeError("Saved original answer awaits application")
            return put(table, *args, **kwargs)
        with patch.object(self.memory, "put", side_effect=interrupted), self.assertRaises(RuntimeError):
            run_flow(self.memory, self.runner, as_of=NOW)
        original_job = next(iter(self.memory.snapshot()["jobs"]))
        capture(self.memory, [{"ad_archive_id": "9001", "page_id": "9002", "is_active": True,
            "start_date": 1788220800, "snapshot": {"title": "Fresh fictional holder"}}], acquisition="fresh-after-answer")
        turn = self.spent_turn()
        job_ids = set(self.memory.snapshot()["jobs"])
        with bind_work_turn(self.memory, turn["id"]):
            result = run_flow(self.memory, self.runner, as_of=NOW)
        state = self.memory.snapshot()
        self.assertEqual(len(state["descriptions"]), 4)
        self.assertTrue(all(row["job_id"] == original_job for row in state["descriptions"].values()))
        self.assertEqual(len(result["products"]), 2, "Saved descriptions must still create deterministic new identities")
        self.assertEqual(set(state["jobs"]), job_ids)
        self.assertEqual(state["batch_runs"][turn["id"]]["counters"]["model_executions"], 1)
        self.assertEqual(state["problems"], {})
        self.assertEqual(self.runner.calls, [])
        self.assertEqual({row["stage"] for row in result["preparation_waits"]},
                         {"description", "identity", "assessment"})

    def test_fresh_wait_preserves_exact_original_items_from_another_runner(self):
        from product_radar.research_flow import _fresh_model_wait
        capture(self.memory)
        run_flow(self.memory, self.runner, as_of=NOW)
        original = next(row["request"]["payload"] for row in self.memory.snapshot()["jobs"].values()
                        if row["request"]["role"] == "E")
        turn = self.spent_turn()
        with bind_work_turn(self.memory, turn["id"]):
            state = self.memory.snapshot()
            reason, fresh = _fresh_model_wait(self.memory, state, "E", original)
            changed = deepcopy(original)
            changed["items"][0]["description"]["label"] += " changed input"
            _, changed_fresh = _fresh_model_wait(self.memory, state, "E", changed)
        self.assertEqual(reason, "WORK_TURN_MODEL_LIMIT")
        self.assertEqual(fresh, set())
        self.assertEqual(changed_fresh, {changed["items"][0]["id"]})

    def test_spent_turn_recovers_saved_fit_sibling_with_its_original_runner(self):
        from test_fit_review import FitRunner
        from product_radar.fit_policy import FIT_POLICY_ID
        capture(self.memory)
        runner = FitRunner()
        commit = self.memory.commit
        applications = 0
        def interrupt_second(event_id, changes, *, validate=None):
            nonlocal applications
            if any(row["table"] == "assessments" for row in changes):
                applications += 1
                if applications == 2:
                    raise SafetyError("SYNTHETIC_LOCAL_APPLICATION_WAIT")
            return commit(event_id, changes, validate=validate)
        with patch.object(self.memory, "commit", side_effect=interrupt_second):
            run_flow(self.memory, runner, fit_policy_id=FIT_POLICY_ID, as_of=NOW)
        self.assertEqual(len(self.memory.snapshot()["assessments"]), 1)
        turn = self.spent_turn()
        jobs = self.memory.snapshot()["jobs"]
        other = FitRunner(); other.identity = "different-current-fit-runner"
        with bind_work_turn(self.memory, turn["id"]):
            run_flow(self.memory, other, limit=1, fit_policy_id=FIT_POLICY_ID, as_of=NOW)
        state = self.memory.snapshot()
        self.assertEqual(len(state["assessments"]), 2)
        self.assertEqual(state["jobs"], jobs)
        self.assertEqual(state["batch_runs"][turn["id"]]["counters"]["model_executions"], 1)
        self.assertEqual(other.calls, [])
        self.assertEqual(sum(role == "E" for role, _ in runner.calls), 1)

    def brand_review(self, concept_id):
        concept = self.memory.snapshot()["concepts"][concept_id]
        return {"concept_id": concept_id, "expected_description_hash": digest(concept["description"]),
                "reason": "Synthetic operator review identifies an established brand.",
                "source_url": "https://example.com/brands", "source_excerpt": "Synthetic established brand listing.",
                "reviewer": "test_operator", "reviewed_at": NOW}

    def test_summary_omits_redirected_concept_without_source_dates(self):
        from product_radar.research_memory import change
        capture(self.memory)
        run_flow(self.memory, self.runner, as_of=NOW)
        state = self.memory.snapshot()
        survivor, duplicate = list(state["concepts"])
        changes = [change("concepts", duplicate,
                          dict(state["concepts"][duplicate], status="merged", merged_into=survivor),
                          state["concepts"][duplicate])]
        for child_id, row in state["associations"].items():
            if row.get("concept_id") == duplicate:
                changes.append(change("associations", child_id, dict(row, concept_id=survivor), row))
        self.memory.commit("fixture-redirect", changes)
        result = candidate_summary(self.memory, as_of=NOW)
        self.assertEqual([row["id"] for row in result["products"]], [survivor])
        self.assertEqual([row["concept_id"] for row in result["catalogue_seeds"]], [survivor])
        # The expanded survivor evidence needs its own current judgment.
        self.assertIsNone(result["products"][0]["checklist_meets"])
        self.assertEqual(result["products"][0]["criteria"], [])
        self.assertEqual(self.memory.snapshot()["assessments"], state["assessments"])

    def test_summary_does_not_present_stale_checklist_after_description_change(self):
        capture(self.memory)
        run_flow(self.memory, self.runner, as_of=NOW)
        state = self.memory.snapshot()
        concept_id = next(iter(state["concepts"]))
        original = state["concepts"][concept_id]
        changed = deepcopy(original)
        changed["description"]["features"].append("Fixture changed mechanism facts")
        self.memory.put("concepts", concept_id, changed, previous=original)
        row = next(row for row in candidate_summary(self.memory, as_of=NOW)["products"] if row["id"] == concept_id)
        self.assertEqual(row["status"], "assessment_pending")
        self.assertIsNone(row["checklist_meets"])
        self.assertEqual(row["market"]["families"]["meta"]["state"], "Insufficient")
        self.assertEqual(self.memory.snapshot()["assessments"], state["assessments"])

    def test_brand_review_preserves_original_jobs_and_repeats_without_new_work(self):
        capture(self.memory)
        run_flow(self.memory, self.runner, as_of=NOW)
        before = self.memory.snapshot()
        concept_id = next(iter(before["concepts"]))
        review = self.brand_review(concept_id)
        self.assertTrue(exclude_established_brand(self.memory, review)["applied"])
        after = self.memory.snapshot()
        self.assertEqual(after["descriptions"], before["descriptions"])
        self.assertEqual(after["assessments"], before["assessments"])
        self.assertEqual(after["concepts"][concept_id]["description"]["brand"], "established")
        self.assertEqual(after["concepts"][concept_id]["brand_review"]["method"], "operator_source_review")
        self.assertFalse(exclude_established_brand(self.memory, review)["applied"])
        calls = len(self.runner.calls)
        result = run_flow(self.memory, self.runner, as_of=NOW)
        self.assertEqual(len(self.runner.calls), calls)
        product = next(p for p in result["products"] if p["id"] == concept_id)
        self.assertEqual(product["status"], "excluded_established_brand")
        self.assertIn("do not pursue", product["market_follow_up"])

    def test_brand_review_rejects_stale_description_without_mutating_memory(self):
        capture(self.memory)
        run_flow(self.memory, self.runner, as_of=NOW)
        concept_id = next(iter(self.memory.snapshot()["concepts"]))
        review = self.brand_review(concept_id)
        review["expected_description_hash"] = "0" * 64
        before = self.memory.snapshot()
        with self.assertRaisesRegex(SafetyError, "BRAND_REVIEW_DESCRIPTION_CHANGED"):
            exclude_established_brand(self.memory, review)
        self.assertEqual(self.memory.snapshot(), before)

    def test_brand_review_changes_draft_scope_and_skips_all_selection_followups(self):
        from product_radar.report_selection import build_report_pool, selection_scope
        capture(self.memory)
        run_flow(self.memory, self.runner, as_of=NOW)
        before = self.memory.snapshot()
        concept_id, neighbor = list(before["concepts"])
        old_scope = digest(selection_scope(before, concept_id))
        exclude_established_brand(self.memory, self.brand_review(concept_id))
        after = self.memory.snapshot()
        self.assertNotEqual(digest(selection_scope(after, concept_id)), old_scope)
        self.assertEqual(after["concepts"][neighbor], before["concepts"][neighbor])
        pool = build_report_pool(self.memory, as_of=NOW)
        decision = next(row for row in pool["decisions"] if row["concept_id"] == concept_id)
        self.assertEqual(decision["decision"], "excluded")
        self.assertIn("established_brand", decision["codes"])
        self.assertIn("Source-backed operator review", decision["reasons"][0])
        for queue in ("needed_market_work", "needed_supplier_work", "needed_fact_work"):
            self.assertFalse(any(row["concept_id"] == concept_id for row in pool[queue]))

    def test_brand_review_rejects_missing_evidence_or_invalid_source(self):
        capture(self.memory)
        run_flow(self.memory, self.runner, as_of=NOW)
        review = self.brand_review(next(iter(self.memory.snapshot()["concepts"])))
        before = self.memory.snapshot()
        for changes in ({"source_excerpt": ""}, {"source_url": "http://example.com"},
                        {"source_url": "https://user:password@example.com"}, {"source_url": "https://[broken"},
                        {"expected_description_hash": "bad"}, {"reviewer": ""}):
            with self.subTest(changes=changes), self.assertRaises(SafetyError):
                exclude_established_brand(self.memory, dict(review, **changes))
        self.assertEqual(self.memory.snapshot(), before)

    def test_connected_flow_and_repeat_reuses_all_results(self):
        self.assertEqual(capture(self.memory)["new_observations"], 4)
        result = run_flow(self.memory, self.runner, as_of=NOW)
        self.assertEqual(len(result["products"]), 2)
        self.assertEqual(sorted(p["observations"] for p in result["products"]), [1, 2])
        self.assertTrue(all(p["checklist_meets"] == 8 for p in result["products"]))
        self.assertTrue(all(p["criteria"][2]["state"] == "Not assessed" for p in result["products"]))
        self.assertTrue(all(not p["client_ready"] for p in result["products"]))
        self.assertEqual([role for role, _ in self.runner.calls].count("A"), 1)
        self.assertEqual([role for role, _ in self.runner.calls].count("E"), 1)
        calls = len(self.runner.calls)
        self.assertTrue(capture(self.memory)["reused_capture"])
        self.assertEqual(run_flow(self.memory, self.runner, as_of=NOW), result)
        self.assertEqual(len(self.runner.calls), calls)
        self.assertFalse(self.journal.path.exists(), "Research must not alter the financial ledger")

    def test_recovered_description_failures_are_not_current_blocks(self):
        capture(self.memory)
        item_id = next(iter(self.memory.snapshot()["observations"].values()))["semantic_id"]
        problem(self.memory, "description", item_id, "AI_LOCAL_PERMISSION_DENIED")
        result = run_flow(self.memory, self.runner, as_of=NOW)
        self.assertEqual(result["unresolved_issues"], 0)
        self.assertEqual(result["recovered_issues"], 1)
        self.assertEqual(result["problems"][0]["current_status"], "recovered")
        self.assertNotIn("current_status", next(iter(self.memory.snapshot()["problems"].values())))

    def test_working_example_explains_missing_market_check(self):
        capture(self.memory)
        result = run_flow(self.memory, self.runner, as_of=NOW)
        self.assertTrue(all("Amazon" in p["market_follow_up"] for p in result["products"]))
        write_summary(self.memory, result)
        page = (self.memory.root / "candidate_summary.html").read_text()
        self.assertIn("fictional", page)
        self.assertIn("Next market check", page)
        self.assertIn("Needs another market check", page)
        self.assertTrue(all(p["supplier_status"] == "not_evaluated_in_this_view" for p in result["products"]))
        self.assertIn("does not say whether a supplier search is complete", page)

    def test_below_threshold_summary_does_not_push_more_market_research(self):
        capture(self.memory)
        real = self.runner
        def fewer_matches(role, payload, folder):
            response, usage = real(role, payload, folder)
            if role == "E":
                for result in response["results"]:
                    for criterion in result["criteria"]:
                        if criterion["id"] == 9:
                            criterion["state"] = "Unclear"
                        if criterion["id"] == 8:
                            criterion["state"] = "Unclear"
            return response, usage
        result = run_flow(self.memory, fewer_matches, as_of=NOW)
        self.assertTrue(all(p["checklist_meets"] == 6 for p in result["products"]))
        self.assertTrue(all("review missing product facts" in p["market_follow_up"] for p in result["products"]))

    def test_assessment_recovery_is_bound_to_the_failed_input_revision(self):
        capture(self.memory)
        run_flow(self.memory, self.runner, as_of=NOW)
        concept_id, assessment = next(iter(self.memory.snapshot()["assessments"].items()))
        problem(self.memory, "assessment", concept_id, "AI_NETWORK_UNAVAILABLE", revision=assessment["input_hash"])
        problem(self.memory, "assessment", concept_id, "AI_NETWORK_UNAVAILABLE", revision="a-new-unassessed-input")
        result = run_flow(self.memory, self.runner, as_of=NOW)
        self.assertEqual(result["unresolved_issues"], 1)
        self.assertEqual(result["recovered_issues"], 1)

    def market_recovery_fixture(self, *, relationship="exact_product", review_changes=None):
        from product_radar.market_validation import REVIEW_VERSION
        capture(self.memory)
        run_flow(self.memory, self.runner, as_of=NOW)
        state = self.memory.snapshot()
        concept_id, concept = next(iter(state["concepts"].items()))
        observation_id = next(row["id"] for row in state["assessments"][concept_id]["result"]["relevance"])
        revision = digest({"synthetic_market_input": concept_id})
        problem(self.memory, "market_relevance", concept_id, "AI_LOCAL_PERMISSION_DENIED", revision=revision)
        review = {"concept_id": concept_id, "description_hash": digest(concept["description"]),
                  "input_hash": revision, "review_version": REVIEW_VERSION, "reviewed_at": NOW,
                  "relevance": [{"id": observation_id, "relationship": relationship,
                                 "reason": "Synthetic accepted relevance verdict."}]}
        review.update(review_changes or {})
        self.memory.put("market_reviews", "fixture-market-review", review)
        return concept_id, review

    def test_market_relevance_recovery_matches_exact_input_without_changing_problem_history(self):
        self.market_recovery_fixture()
        before = self.memory.snapshot()
        result = candidate_summary(self.memory, as_of=NOW)
        self.assertEqual(result["recovered_issues"], 1)
        self.assertEqual(result["unresolved_issues"], 0)
        self.assertEqual(result["problems"][0]["current_status"], "recovered")
        self.assertEqual(self.memory.snapshot(), before)
        self.assertNotIn("current_status", next(iter(before["problems"].values())))

    def test_unrelated_market_verdict_recovers_execution_failure_without_positive_demand(self):
        concept_id, _ = self.market_recovery_fixture(relationship="unrelated")
        result = candidate_summary(self.memory, as_of=NOW)
        self.assertEqual(result["recovered_issues"], 1)
        product = next(p for p in result["products"] if p["id"] == concept_id)
        self.assertFalse(product["market"]["families"]["marketplace"]["available"])
        self.assertIsNot(product["market"]["families"]["marketplace"]["positive"], True)
        self.assertEqual(product["market"]["positive_families"], 0)
        self.assertFalse(product["client_ready"])

    def test_changed_market_input_does_not_recover_a_different_failed_revision(self):
        self.market_recovery_fixture(review_changes={"input_hash": "different-input-hash"})
        result = candidate_summary(self.memory, as_of=NOW)
        self.assertEqual(result["recovered_issues"], 0)
        self.assertEqual(result["unresolved_issues"], 1)

    def test_changed_product_description_does_not_recover_old_market_input(self):
        concept_id, _ = self.market_recovery_fixture()
        old = self.memory.snapshot()["concepts"][concept_id]
        changed = deepcopy(old)
        changed["description"]["function"] = "A different synthetic function"
        self.memory.put("concepts", concept_id, changed, previous=old)
        result = candidate_summary(self.memory, as_of=NOW)
        self.assertEqual(result["recovered_issues"], 0)
        self.assertEqual(result["unresolved_issues"], 1)

    def test_old_market_review_version_does_not_recover_current_failure(self):
        self.market_recovery_fixture(review_changes={"review_version": "marketplace-relevance-old-fixture"})
        result = candidate_summary(self.memory, as_of=NOW)
        self.assertEqual(result["recovered_issues"], 0)
        self.assertEqual(result["unresolved_issues"], 1)

    def test_future_market_review_does_not_recover_before_its_as_of_time(self):
        self.market_recovery_fixture(review_changes={"reviewed_at": "2026-10-06T10:00:00+00:00"})
        result = candidate_summary(self.memory, as_of=NOW)
        self.assertEqual(result["recovered_issues"], 0)
        self.assertEqual(result["unresolved_issues"], 1)
        later = candidate_summary(self.memory, as_of="2026-10-06T10:00:00+00:00")
        self.assertEqual(later["recovered_issues"], 1)

    def test_market_review_for_another_concept_does_not_recover_target_failure(self):
        concept_id, review = self.market_recovery_fixture()
        state = self.memory.snapshot()
        neighbor_id, neighbor = next((i, c) for i, c in state["concepts"].items() if i != concept_id)
        other = dict(review, concept_id=neighbor_id, description_hash=digest(neighbor["description"]))
        self.memory.put("market_reviews", "fixture-market-review", other, previous=review)
        result = candidate_summary(self.memory, as_of=NOW)
        self.assertEqual(result["recovered_issues"], 0)
        self.assertEqual(result["unresolved_issues"], 1)

    def test_new_collection_identical_content_refreshes_date_not_inference(self):
        capture(self.memory)
        first = run_flow(self.memory, self.runner, as_of=NOW)
        calls = len(self.runner.calls)
        capture(self.memory, acquisition="fixture2", observed=NOW)
        second = run_flow(self.memory, self.runner, as_of=NOW)
        self.assertEqual(len(self.runner.calls), calls)
        self.assertEqual(second["observations"], 8)
        self.assertEqual([p["id"] for p in second["products"]], [p["id"] for p in first["products"]])
        self.assertTrue(all(p["last_observed"] == NOW and p["first_seen"] == OBSERVED for p in second["products"]))
        # Repeat rows do not multiply ad counts.
        self.assertEqual([p["market"]["families"]["meta"]["active_ads"] for p in second["products"]], [2, 1])

    def test_checklist_has_practical_rubric_without_unrequested_prior_judgments(self):
        capture(self.memory)
        run_flow(self.memory, self.runner, as_of=NOW)
        payload = next(p for role, p in self.runner.calls if role == "E")
        self.assertEqual(payload["checklist_scope"], CHECKLIST_SCOPE)
        self.assertNotIn("earlier_judgments", payload)
        self.assertTrue(all(item["earlier_judgments"] == [] for item in payload["items"]))
        self.assertIn("not a demand for measured proof", payload["checklist_rules"])
        self.assertIn("do not invent dimensions", payload["checklist_rules"])
        self.assertIn("Do not preserve an unsupported past judgment", payload["checklist_rules"])
        self.assertTrue(all("checklist_rules" not in p for role, p in self.runner.calls if role != "E"))

    def test_changed_evidence_supplies_previous_checklist_but_keeps_fact_hash_stable(self):
        capture(self.memory)
        run_flow(self.memory, self.runner, as_of=NOW)
        before = self.memory.snapshot()["assessments"]
        capture(self.memory, [{"ad_archive_id": "999", "page_id": "299", "is_active": True,
                               "start_date": 1788220800, "snapshot": {"title": "Green sink caddy"}}],
                acquisition="new-seller", observed=NOW)
        run_flow(self.memory, self.runner, as_of=NOW)
        payload = [p for role, p in self.runner.calls if role == "E"][-1]
        self.assertEqual(len(payload["items"]), 1, "Unrelated product assessments must remain reusable")
        item = payload["items"][0]
        self.assertEqual(item["earlier_judgments"], before[item["id"]]["result"]["criteria"])
        current = self.memory.snapshot()["assessments"][item["id"]]
        facts = {field: value for field, value in item.items() if field != "earlier_judgments"}
        self.assertEqual(current["input_hash"], digest(facts), "Selection hashes current source facts, not the previous verdict")
        calls = len(self.runner.calls)
        run_flow(self.memory, self.runner, as_of=NOW)
        self.assertEqual(len(self.runner.calls), calls, "Earlier judgments must not create a rescore loop")

    def test_previous_checklist_does_not_force_a_passing_score(self):
        capture(self.memory)
        run_flow(self.memory, self.runner, as_of=NOW)
        capture(self.memory, [{"ad_archive_id": "999", "page_id": "299", "is_active": True,
                               "start_date": 1788220800, "snapshot": {"title": "Green sink caddy"}}],
                acquisition="new-seller", observed=NOW)
        def corrected(role, payload, folder):
            response, usage = self.runner(role, payload, folder)
            if role == "E":
                for result in response["results"]:
                    for criterion in result["criteria"]:
                        if criterion["id"] == 9:
                            criterion.update(state="Unclear", basis="not_stated", reason="Earlier avoided-expense inference was unsupported.")
            return response, usage
        run_flow(self.memory, corrected, as_of=NOW)
        payload = [p for role, p in self.runner.calls if role == "E"][-1]
        concept_id = payload["items"][0]["id"]
        self.assertEqual(payload["items"][0]["earlier_judgments"][8]["state"], "Meets")
        self.assertEqual(self.memory.snapshot()["assessments"][concept_id]["result"]["criteria"][8]["state"], "Unclear")

    def test_changed_checklist_saved_answer_survives_interrupted_application(self):
        capture(self.memory)
        run_flow(self.memory, self.runner, as_of=NOW)
        capture(self.memory, [{"ad_archive_id": "999", "page_id": "299", "is_active": True,
                               "start_date": 1788220800, "snapshot": {"title": "Green sink caddy"}}],
                acquisition="new-seller", observed=NOW)
        put = self.memory.put
        def interrupted(table, *args, **kwargs):
            if table == "assessments":
                raise RuntimeError("stop after saving changed checklist")
            return put(table, *args, **kwargs)
        with patch.object(self.memory, "put", side_effect=interrupted), self.assertRaisesRegex(RuntimeError, "stop after saving"):
            run_flow(self.memory, self.runner, as_of=NOW)
        calls = len(self.runner.calls)
        run_flow(self.memory, self.runner, as_of=NOW)
        self.assertEqual(len(self.runner.calls), calls, "Reuse the saved answer and its original prior-judgment context")

    def test_partial_checklist_application_reuses_remaining_sibling_in_smaller_batch(self):
        capture(self.memory)
        run_flow(self.memory, self.runner, as_of=NOW)
        capture(self.memory, [{"ad_archive_id": "999", "page_id": "299", "is_active": True,
                               "start_date": 1788220800, "snapshot": {"title": "Green sink caddy"}},
                              {"ad_archive_id": "998", "page_id": "298", "is_active": True,
                               "start_date": 1788220800, "snapshot": {"title": "Powered cleaning brush waterproof"}}],
                acquisition="new-sellers", observed=NOW)
        put = self.memory.put
        applied = []
        def interrupted(table, *args, **kwargs):
            if table == "assessments":
                if applied:
                    raise RuntimeError("stop before second checklist application")
                applied.append(args[0])
            return put(table, *args, **kwargs)
        with patch.object(self.memory, "put", side_effect=interrupted), self.assertRaisesRegex(RuntimeError, "second checklist"):
            run_flow(self.memory, self.runner, as_of=NOW)
        completed_batch = [p for role, p in self.runner.calls if role == "E"][-1]
        self.assertEqual(len(completed_batch["items"]), 2)
        calls = len(self.runner.calls)
        run_flow(self.memory, self.runner, limit=1, as_of=NOW)
        self.assertEqual(len(self.runner.calls), calls, "Removing a completed sibling must not change the remaining item's binding")
        self.assertEqual(len(self.memory.snapshot()["assessments"]), 2)
        run_flow(self.memory, self.runner, as_of=NOW)
        self.assertEqual(len(self.runner.calls), calls)

    def test_changed_capture_same_acquisition_is_conflict(self):
        capture(self.memory)
        changed = rows()
        changed[0]["snapshot"]["title"] = "Different mechanism"
        with self.assertRaisesRegex(SafetyError, "CAPTURE_CONFLICT"):
            capture(self.memory, changed)
        self.assertEqual(len(self.memory.snapshot()["observations"]), 4)

    def test_restart_after_saved_model_response_does_not_call_model_again(self):
        capture(self.memory)
        original = self.memory.put
        def fail(table, *args, **kwargs):
            if table == "descriptions":
                raise RuntimeError("simulated stop after saved response")
            return original(table, *args, **kwargs)
        with patch.object(self.memory, "put", side_effect=fail), self.assertRaises(RuntimeError):
            run_flow(self.memory, self.runner, as_of=NOW)
        self.assertEqual(len(self.runner.calls), 1)
        result = run_flow(self.memory, self.runner, as_of=NOW)
        self.assertEqual(len(result["products"]), 2)
        self.assertEqual([r for r, _ in self.runner.calls].count("A"), 1)

    def test_committed_events_survive_missing_or_corrupt_index(self):
        capture(self.memory)
        atomic_json(self.memory.root / "index.json", {"broken": True})
        self.assertEqual(len(self.memory.snapshot()["observations"]), 4)
        event = next((self.memory.root / "events").glob("*.json"))
        atomic_json(event, {"broken": True})
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
            self.memory.snapshot()

    def test_stale_change_rejected_and_same_event_applies_once(self):
        self.memory.put("concepts", "c1", {"value": 1})
        old = {"value": 1}
        self.memory.put("concepts", "c1", {"value": 2}, previous=old)
        with self.assertRaisesRegex(SafetyError, "DEPENDENCY_CHANGED"):
            self.memory.put("concepts", "c1", {"value": 3}, previous=old)
        self.assertFalse(self.memory.put("concepts", "c1", {"value": 2}, previous=old))

    def test_model_does_not_hold_financial_lock(self):
        capture(self.memory)
        memory, runner = self.memory, self.runner
        def unlocked(role, payload, folder):
            with memory.journal.locked():
                pass
            return runner(role, payload, folder)
        result = run_flow(memory, unlocked, as_of=NOW)
        self.assertEqual(len(result["products"]), 2)

    def test_invalid_sibling_survives_and_failed_item_does_not_loop(self):
        capture(self.memory)
        real = self.runner
        def bad_one(role, payload, folder):
            response, usage = real(role, payload, folder)
            if role == "A":
                response["results"][0]["products"][0]["category"] = "Pets"
            return response, usage
        result = run_flow(self.memory, bad_one, as_of=NOW)
        self.assertEqual(len(result["products"]), 2)
        self.assertEqual(len(result["problems"]), 1)
        calls = len(real.calls)
        run_flow(self.memory, bad_one, as_of=NOW)
        self.assertEqual(len(real.calls), calls)

    def test_unclear_match_does_not_stop_independent_item(self):
        capture(self.memory)
        real = self.runner
        def unclear(role, payload, folder):
            response, usage = real(role, payload, folder)
            if role == "B":
                response["results"][0].update(decision="unclear", match_id=None, reason="Need clearer mechanism.")
            return response, usage
        result = run_flow(self.memory, unclear, as_of=NOW)
        self.assertEqual(len(result["products"]), 2)
        self.assertEqual(len(result["deferred_items"]), 1)

    def test_html_escapes_source_and_remains_internal(self):
        capture(self.memory)
        result = run_flow(self.memory, self.runner, as_of=NOW)
        result["products"][0]["label"] = "<script>alert(1)</script>"
        write_summary(self.memory, result)
        html = (self.memory.root / "candidate_summary.html").read_text()
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertIn("not a client report", html)

    def test_completion_survives_gap_before_result_wrapper(self):
        payload = {"items": [{"id": "i1", "source_text": {"title": "Blue sink caddy"}}]}
        real = self.runner
        class InterruptRunner:
            identity = real.identity
            def __call__(self, role, supplied, folder):
                response, usage = real(role, supplied, folder)
                atomic_json(folder / "completion.json", {"response": response, "usage": usage, "exit_code": 0})
                raise SafetyError("AI_INTERRUPTED_REVIEW_SAVED_JOB")
        with self.assertRaises(SafetyError):
            Jobs(self.memory, InterruptRunner()).run("A", payload)
        _, result = Jobs(self.memory, self.runner).run("A", payload)
        self.assertIn("i1", result["accepted"])
        self.assertEqual(len(real.calls), 1)

    def test_pending_intent_never_launches_again(self):
        payload = {"items": [{"id": "i1", "source_text": {"title": "Blue sink caddy"}}]}
        class Fail:
            identity = "failing-test-v1"
            def __call__(self, *args):
                raise SafetyError("AI_PROVIDER_REQUEST_FAILED")
        jobs = Jobs(self.memory, Fail())
        with self.assertRaisesRegex(SafetyError, "PROVIDER_REQUEST_FAILED"):
            jobs.run("A", payload)
        with self.assertRaisesRegex(SafetyError, "PENDING_RECONCILIATION"):
            jobs.run("A", payload)

    def test_changed_batch_size_reuses_completed_item(self):
        first = {"id": "one", "source_text": {"title": "Blue sink caddy"}}
        second = {"id": "two", "source_text": {"title": "Pink sink caddy"}}
        jobs = Jobs(self.memory, self.runner)
        jobs.run("A", {"items": [first, second]})
        _, result = jobs.run("A", {"items": [second]})
        self.assertEqual(set(result["accepted"]), {"two"})
        self.assertEqual(len(self.runner.calls), 1)

    def test_changed_item_cannot_inherit_old_accepted_result_on_failure(self):
        first = {"id": "one", "source_text": {"title": "Blue sink caddy"}}
        second = {"id": "two", "source_text": {"title": "Pink sink caddy"}}
        real = self.runner
        def selective(role, payload, folder):
            response, usage = real(role, payload, folder)
            if any(item["source_text"]["title"] == "Changed" for item in payload["items"]):
                response["results"][0]["kind"] = "invalid"
            return response, usage
        jobs = Jobs(self.memory, selective)
        jobs.run("A", {"items": [first, second]})
        changed = dict(first, source_text={"title": "Changed"})
        _, result = jobs.run("A", {"items": [changed, second]})
        self.assertNotIn("one", result["accepted"])
        self.assertIn("one", result["failures"])
        self.assertIn("two", result["accepted"])

    def test_explicit_retry_keeps_failed_attempt_and_completed_retries_reuse(self):
        payload = {"items": [{"id": "one", "source_text": {"title": "Blue sink caddy"}}]}
        real = self.runner
        class FailsOnce:
            identity = "fails-once-test-v1"
            failed = False
            def __call__(self, *args):
                if not self.failed:
                    self.failed = True
                    raise SafetyError("AI_NETWORK_UNAVAILABLE")
                return real(*args)
        jobs = Jobs(self.memory, FailsOnce())
        with self.assertRaises(SafetyError):
            jobs.run("A", payload)
        job_id = next(iter(self.memory.snapshot()["jobs"]))
        prepare_retry(self.memory, job_id, "Fictional network restoration.")
        jobs.run("A", payload)
        jobs.run("A", payload)
        self.assertEqual(len(real.calls), 1)
        self.assertTrue((self.memory.root / "ai_jobs" / job_id / "failure.json").exists())
        with self.assertRaisesRegex(SafetyError, "COMPLETED_RESULT_REUSE"):
            prepare_retry(self.memory, job_id, "Should be refused.")

    def test_saved_attempt_bare_preparation_is_imported_without_credentials(self):
        import test_meta_live_trial as helpers
        from product_radar.models import prepared_request
        from product_radar.providers.apify import map_prepared_rows
        prepared = prepared_request(helpers.cli.prepare(helpers.CONFIG, "test-import")["request"])
        values = rows()
        folder = self.journal.root / "test-import"
        atomic_json(folder / "prepared.json", prepared.to_dict())
        atomic_json(folder / "captured_rows.json", values)
        atomic_json(folder / "mapped_rows.json", map_prepared_rows(values, prepared))
        atomic_json(folder / "capture_manifest.json", {"captured_hash": digest(values), "dataset_id": "fixture-dataset", "observed_at": OBSERVED})
        attempt = {"capture": digest(values), "dataset_id": "fixture-dataset", "config_hash": prepared.config_hash}
        memory = ResearchMemory(self.root / "captured-shape-test", self.journal)
        with patch.object(self.journal, "state", return_value=({}, {"test-import": attempt})):
            self.assertEqual(import_saved_attempt(memory, "test-import")["new_observations"], 4)
            self.assertTrue(import_saved_attempt(memory, "test-import")["reused_capture"])

    def test_mixed_creative_children_share_one_parent_ad(self):
        values = rows()[:1]
        values[0]["snapshot"] = {"title": "Mixed catalogue", "cards": [{"title": "Blue sink caddy"}, {"title": "Pink sink caddy"}]}
        capture(self.memory, values)
        result = run_flow(self.memory, self.runner, as_of=NOW)
        self.assertEqual(len(result["products"]), 1)
        self.assertEqual(result["products"][0]["market"]["families"]["meta"]["active_ads"], 1)

    def test_mixed_text_produces_multiple_concepts_without_copying_parent_price(self):
        capture(self.memory, rows()[:1])
        real = self.runner
        def mixed(role, payload, folder):
            response, usage = real(role, payload, folder)
            if role == "A":
                first = response["results"][0]["products"][0]
                second = dict(first, label="Walking stick", function="Support walking", mechanism="folding")
                response["results"][0].update(kind="mixed", products=[first, second])
            return response, usage
        result = run_flow(self.memory, mixed, as_of=NOW)
        self.assertEqual(len(result["products"]), 2)
        self.assertTrue(all(p["prices"] == [] for p in result["products"]))


class ContractTests(unittest.TestCase):
    def test_two_usable_sources_without_positive_signal_cannot_advance(self):
        self.assertEqual(research_status(8, {"available_families": 2, "positive_families": 0}), "weak_market_signal")
        self.assertEqual(research_status(8, {"available_families": 2, "positive_families": 1}), "candidate_for_supplier_review")
        self.assertEqual(research_status(8, {"available_families": 1, "positive_families": 1}), "needs_market_evidence")
        self.assertEqual(research_status(6, {"available_families": 2, "positive_families": 1}), "below_checklist_threshold")

    def test_market_follow_up_does_not_require_all_three_sources(self):
        market = {"families": {"meta": {"available": True}, "marketplace": {"available": True},
                               "trends": {"available": False}}, "positive_families": 1}
        self.assertIn("sufficient", market_follow_up(market))
        market["positive_families"] = 0
        self.assertIn("set this product aside", market_follow_up(market))

    def test_single_source_follow_up_distinguishes_coverage_from_positive_signal(self):
        market = {"families": {"meta": {"available": True}, "marketplace": {"available": False},
                               "trends": {"available": False}}, "positive_families": 0}
        original = deepcopy(market)
        self.assertIn("Another usable check alone does not qualify", market_follow_up(market))
        self.assertEqual(market, original)
        market["positive_families"] = 1
        self.assertIn("does not have to be positive", market_follow_up(market))
        market["positive_families"] = 0
        market["families"]["meta"]["available"] = False
        self.assertIn("Two usable independent sources are needed", market_follow_up(market))

    def test_margin_removed_missing_criteria_filled_invalid_refs_reject_item(self):
        payload = {"items": [{"id": "c", "observations": [{"id": "o"}]}]}
        result = {"id": "c", "criteria": [{"id": 3, "state": "Meets", "basis": "inferred", "references": [], "reason": "Bad margin."}],
                  "relevance": [], "compliance_note": "", "missing_questions": []}
        checked = validate_results("E", payload, {"results": [result]})
        rows = checked["accepted"]["c"]["criteria"]
        self.assertEqual(len(rows), 13)
        self.assertEqual(rows[2]["state"], "Not assessed")
        self.assertEqual(rows[0]["state"], "Unclear")
        result["criteria"][0]["references"] = ["invented"]
        self.assertIn("c", validate_results("E", payload, {"results": [result]})["failures"])

    def test_unknown_and_duplicate_match_ids_are_not_applied(self):
        payload = {"items": [{"id": "child", "candidates": [{"id": "known"}]}]}
        answer = {"id": "child", "decision": "same", "match_id": "invented", "reason": "Guess"}
        self.assertIn("child", validate_results("B", payload, {"results": [answer]})["failures"])
        answer["match_id"] = "known"
        self.assertIn("child", validate_results("B", payload, {"results": [answer, answer]})["failures"])

    def test_previous_judgment_references_are_not_current_source_evidence(self):
        earlier = {"id": 6, "state": "Meets", "basis": "inferred", "references": ["old"],
                   "reason": "Earlier convenience judgment."}
        payload = {"items": [{"id": "c", "observations": [{"id": "current"}], "earlier_judgments": [earlier]}]}
        result = {"id": "c", "criteria": [earlier], "relevance": [], "compliance_note": "", "missing_questions": []}
        rejected = validate_results("E", payload, {"results": [result]})
        self.assertNotIn("c", rejected["accepted"])
        self.assertIn("AI_EVIDENCE_REFERENCE_INVALID", rejected["failures"]["c"])
        result["criteria"] = [dict(earlier, references=["current"])]
        self.assertIn("c", validate_results("E", payload, {"results": [result]})["accepted"])

    def test_codex_tool_output_rejected_missing_usage_not_zero(self):
        events = [{"type": "turn.started"}, {"type": "item.completed", "item": {"type": "agent_message", "text": '{"results":[]}'}}, {"type": "turn.completed"}]
        raw = b"\n".join(canonical(e) for e in events)
        self.assertIsNone(parse_codex_output(raw)[1])
        events[1]["item"]["type"] = "command_execution"
        with self.assertRaisesRegex(SafetyError, "TOOL_ACTIVITY"):
            parse_codex_output(b"\n".join(canonical(e) for e in events))

    def test_missing_market_information_is_not_zero_demand(self):
        signals = compute_market_signals({}, {}, NOW)
        self.assertEqual(signals["label"], "Insufficient evidence")
        self.assertIsNone(signals["families"]["meta"]["advertisers"])
        self.assertIsNone(signals["families"]["trends"]["positive"])

    def test_positive_meta_or_branch_does_not_require_missing_dates(self):
        observations = {str(n): {"source": "meta", "source_item": str(n), "semantic_id": "s", "observed_at": OBSERVED,
                        "facts": {"ad_id": str(n), "advertiser_id": str(n), "active": None, "started_at": None}} for n in range(3)}
        result = compute_market_signals(observations, {"s": "exact_product"}, NOW)
        self.assertEqual(result["families"]["meta"]["state"], "Positive")

    def test_changed_source_identity_does_not_keep_old_positive_evidence(self):
        observations = {}
        for n in range(3):
            base = {"source": "meta", "source_item": str(n), "semantic_id": "old", "observed_at": OBSERVED,
                    "facts": {"ad_id": str(n), "advertiser_id": str(n), "active": True, "started_at": None}}
            observations["old-" + str(n)] = base
            observations["new-" + str(n)] = dict(base, semantic_id="different-product", observed_at=NOW)
        result = compute_market_signals(observations, {"old": "exact_product"}, NOW)
        self.assertEqual(result["families"]["meta"]["state"], "Insufficient")

    def test_stale_observations_do_not_support_market_signal(self):
        observations = {str(n): {"source": "meta", "source_item": str(n), "semantic_id": "s", "observed_at": "2026-09-01T00:00:00+00:00",
                        "facts": {"ad_id": str(n), "advertiser_id": str(n), "active": True, "started_at": "2026-08-01T00:00:00+00:00"}} for n in range(3)}
        self.assertEqual(compute_market_signals(observations, {"s": "exact_product"}, NOW)["available_families"], 0)

    def test_cli_requires_separate_live_model_authority(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/benchmark_providers.py"), "research-run"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("EXPLICIT_LIVE_MODEL_REQUIRED", result.stdout)

    def test_audience_alone_does_not_trigger_identity_comparison(self):
        product = {"label": "Caddy", "function": "Store utensils", "mechanism": "draining", "audience": "adult"}
        other = {"label": "Suitcase", "function": "Carry luggage", "mechanism": "wheeled", "audience": "adult"}
        self.assertFalse(find_possible_matches(product, {"c": {"description": other}})["candidates"])

    def test_retrieval_payload_stays_small_as_history_grows(self):
        product = {"label": "Caddy", "function": "Store utensils", "mechanism": "draining", "audience": "adult"}
        concepts = {"concept-" + str(n): {"description": dict(product, label="Caddy " + str(n))} for n in range(800)}
        result = find_possible_matches(product, concepts)
        self.assertEqual(len(result["candidates"]), 10)
        self.assertEqual(result["omitted"], 790)


if __name__ == "__main__":
    if sys.argv[1:] == ["--write-review"]:
        # Deliberately isolated manufactured example; never captured/client data.
        review_root = ROOT / "data/research/rehearsal"
        memory = ResearchMemory(review_root, Journal(review_root / "fixture-lock"), evidence_type="synthetic")
        capture(memory)
        runner = FakeRunner()
        summary = run_flow(memory, runner, as_of=NOW)
        calls = len(runner.calls)
        repeated = run_flow(memory, runner, as_of=NOW)
        if summary != repeated or len(runner.calls) != calls:
            raise RuntimeError("Rehearsal replay did not reuse completed work")
        write_summary(memory, summary)
        print("Synthetic working example saved: " + str(review_root / "candidate_summary.html"))
        print("Repeat-run additional simulated model calls: 0")
    else:
        unittest.main()
