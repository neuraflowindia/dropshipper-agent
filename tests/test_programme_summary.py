"""Offline summary arithmetic over explicit fixtures; no canonical writes."""

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from test_catalogue_expansion import fixture as catalogue_fixture, phrase
from product_radar.fit_policy import ACTIVE_CRITERION_IDS, FIT_POLICY_ID
from product_radar.fit_review import PRACTICAL_CONTRACT
from product_radar.issue_allocation import allocate_issues
from product_radar.models import digest
from product_radar.programme_summary import programme_cost_capacity_summary
from product_radar.programme_cost_snapshot import build_owner_cost_snapshot
from product_radar.research_flow import assessment_item
from product_radar.storage import POLICY_VERSION

NOW = "2026-10-08T10:00:00+00:00"
PRIVATE = "PRIVATE_PROFILE_ID_MUST_NOT_APPEAR"


def record(ids):
    return {"programme_id": "original-programme", "source_run_id": "original-source-run", "client_id": "default",
        "concept_ids": ids, "follow_on_reviews": {}, "reporting_inventory_concept_ids": [], "updated_at": NOW,
        "release_slots": [{"issue_id": "issue-" + str(n), "date": day, "client_id": "default", "revision": 1}
                          for n, day in enumerate(("2026-10-13", "2026-10-15", "2026-10-17"))]}


def state():
    return {"concepts": {}, "assessments": {}, "observations": {}, "associations": {}, "batch_runs": {},
            "query_tasks": {}, "query_outcomes": {}, "captures": {}, "published_history": {}, "issue_reservations": {}}


def add_review(saved, cid):
    description = {"label": "Sink caddy", "category": "Home and Kitchen", "function": "Store sink tools",
        "mechanism": "Draining tray", "features": [], "brand": "generic_or_small", "audience": "Households",
        "basis": "observed", "reason": "Synthetic description."}
    saved["concepts"][cid] = {"description": description, "status": "active"}
    sid, oid = "semantic-" + cid, "observation-" + cid
    saved["observations"][oid] = {"semantic_id": sid, "source": "meta", "acquisition_id": "old-capture",
        "observed_at": NOW, "semantic": {"title": "Sink caddy", "body": "Stores dishwashing tools."}}
    saved["associations"]["association-" + cid] = {"concept_id": cid, "semantic_id": sid, "status": "resolved"}
    criteria = [{"id": number, "state": "Meets" if n < 6 else "Unclear", "basis": "inferred" if n < 6 else "not_stated",
                 "references": [oid] if n < 6 else [], "reason": "Synthetic saved judgment."}
                for n, number in enumerate(ACTIVE_CRITERION_IDS)]
    saved["assessments"][cid] = {"input_hash": digest(assessment_item(saved, cid)), "fit_policy_id": FIT_POLICY_ID,
                                "contract_version": PRACTICAL_CONTRACT, "result": {"criteria": criteria}}


def product(cid, **changes):
    return {"concept_id": cid, "category": "Home and Kitchen", "identity_reviewed": True, "eligible": True,
        "fit_policy_id": FIT_POLICY_ID, "criterion_ids": list(ACTIVE_CRITERION_IDS), "kpi_total": 10,
        "kpi_states": ["Meets"] * 6 + ["Unclear"] * 4, "kpi_matches": 6, "fit_tier": "good", "status": "New",
        "evidence_refs": ["synthetic-observation"], "usable_until": "2026-10-20T10:00:00+00:00", **changes}


def financial():
    namespace = PRIVATE + ":saved-period"
    budget = {"namespace": namespace, "allowance": "5", "policy": {"version": POLICY_VERSION, "future_allocation": "3"}}
    attempt = {"namespace": namespace, "generation": "synthetic-generation", "stage": "meta", "charge": "0.5",
        "actor_cap": "0.6", "cap": "0.62", "read_charge": None, "read_hold": "0.02", "submission": "submitted",
        "remote_status": "SUCCEEDED", "capture_status": "captured"}
    failed = dict(attempt, charge="0.008", actor_cap="0.2", cap="0.2", read_charge="0", read_hold="0",
                  remote_status="FAILED", capture_status="failed")
    return ({namespace: budget}, {"shared-attempt": attempt, "failed-attempt": failed})


def account_status():
    return {"pool_enabled": False, "configured_financial_accounts": 1, "accounts": [
        {"account_id": PRIVATE, "enabled": True, "state": "saved_account", "opening_usd": "5", "spendable_usd": "1.022"},
        {"account_id": "unsupplied2", "enabled": False, "state": "not_configured", "opening_usd": "0", "spendable_usd": "0"},
        {"account_id": "same-provider-alias", "enabled": True, "state": "same_account_alias", "opening_usd": "5", "spendable_usd": "5"}]}


class ProgrammeSummaryTests(unittest.TestCase):
    def test_missing_inputs_stay_unknown_not_zero_cost_or_unused_searches(self):
        result = programme_cost_capacity_summary(as_of=NOW)
        for section in ("money", "catalogue", "research", "report_supply", "effort", "forecast"):
            self.assertEqual(result[section]["state"], "unknown")
        self.assertEqual(result["accounts"]["approved_accounts"], 3)
        self.assertIsNone(result["accounts"]["configured_accounts"])
        self.assertIsNone(result["per_issue_cash_cost_usd"])
        self.assertEqual(result["forecast"]["assumed_yields"], ["0.05", "0.03"])

    def test_failed_costs_holds_future_allocations_and_shared_cost_are_separate(self):
        result = programme_cost_capacity_summary(financial_state=financial(), as_of=NOW,
            effort_rows={"paid_attempt_ids": ["shared-attempt", "shared-attempt"]})["money"]
        self.assertEqual(Decimal(result["ledger_known_incurred_usd"]), Decimal("0.508"))
        self.assertEqual(Decimal(result["ledger_unsettled_hold_usd"]), Decimal("0.02"))
        self.assertFalse(result["ledger_accounting_complete"])
        self.assertEqual(result["selected_supporting_acquisitions"], 1)
        self.assertEqual(Decimal(result["selected_known_incurred_usd"]), Decimal("0.5"))
        self.assertEqual(Decimal(result["saved_future_source_allocations_usd"]), Decimal("3"))
        self.assertEqual(Decimal(result["saved_unallocated_headroom_all_periods_usd"]), Decimal("1.022"))
        self.assertEqual(result["shared_cost_attribution"], "unallocated")
        self.assertIsNone(result["per_issue_cost_usd"])
        self.assertIsNone(result["incremental_ai_invoice_usd"])

    def test_unknown_actor_charge_is_held_not_reported_as_zero_bill(self):
        ledger = financial(); ledger[1]["failed-attempt"]["charge"] = None
        value = programme_cost_capacity_summary(financial_state=ledger, as_of=NOW)["money"]
        self.assertEqual(Decimal(value["ledger_known_incurred_usd"]), Decimal("0.5"))
        self.assertEqual(Decimal(value["ledger_unsettled_hold_usd"]), Decimal("0.22"))
        self.assertFalse(value["ledger_accounting_complete"])

    def test_bad_optional_cost_selection_does_not_hide_valid_ledger_totals(self):
        value = programme_cost_capacity_summary(financial_state=financial(), as_of=NOW,
            effort_rows={"paid_attempt_ids": ["not-in-the-ledger"]})["money"]
        self.assertEqual(value["state"], "known")
        self.assertEqual(Decimal(value["ledger_known_incurred_usd"]), Decimal("0.508"))
        self.assertEqual(value["selected_supporting_cost_state"], "unknown")
        self.assertIsNone(value["selected_known_incurred_usd"])

    def test_approved_accounts_do_not_turn_missing_or_alias_profiles_into_funds(self):
        value = programme_cost_capacity_summary(account_status=account_status(), as_of=NOW)["accounts"]
        self.assertEqual(value["approved_accounts"], 3)
        self.assertEqual(value["configured_accounts"], 1)
        self.assertEqual(value["saved_funded_accounts"], 1)
        self.assertIsNone(value["currently_verified_funded_accounts"])
        self.assertEqual(Decimal(value["saved_enabled_unallocated_headroom_usd"]), Decimal("1.022"))

    def test_catalogue_exact_choices_count_blocked_attempt_and_not_reason_duplicates(self):
        catalogue = catalogue_fixture(); catalogue["families"][0]["phrases"].extend([
            dict(phrase("SINK CADDY"), reason="Another explanation."),
            dict(phrase("sink caddy"), source="trends", purpose="validation")])
        saved = state()
        question = {"id": "question", "route": "meta-ads", "request": {"query": "sink caddy", "country": "IN"},
                    "choice_purpose": "discovery", "language": "en"}
        saved["batch_runs"]["cycle"] = {"kind": "catalogue-discovery-cycle-v1", "planned_actions": [
            {"family_id": "caddy", "questions": [question]}], "actions": {"question": {
            "state": "blocked", "dispatch_started": True, "capture_ids": [], "acquisition_id": "actual-read", "observed_at": NOW}}}
        value = programme_cost_capacity_summary(catalogue=catalogue, state=saved, financial_state=financial(), as_of=NOW)["catalogue"]
        self.assertEqual(value["totals"]["families"], 1)
        self.assertEqual(value["totals"]["family_phrase_pairs"], 1)
        self.assertEqual(value["totals"]["source_purpose_options"], 2)
        self.assertEqual(value["totals"]["meta_discovery_tried"], 1)
        self.assertEqual(value["totals"]["meta_discovery_not_dispatched"], 0)
        self.assertEqual(value["categories"]["Home and Kitchen"]["source_purpose_counts"], [
            {"source": "meta", "purpose": "discovery", "choices": 1}, {"source": "trends", "purpose": "validation", "choices": 1}])

    def test_inventory_counts_without_history_do_not_claim_unvisited_coverage(self):
        value = programme_cost_capacity_summary(catalogue=catalogue_fixture(), as_of=NOW)["catalogue"]
        self.assertEqual(value["totals"]["families"], 1)
        self.assertIsNone(value["totals"]["meta_discovery_not_dispatched"])
        self.assertIsNone(value["totals"]["meta_discovery_tried"])
        saved = state(); saved["query_tasks"]["paid-question"] = {"attempt_id": "missing-ledger"}
        value = programme_cost_capacity_summary(catalogue=catalogue_fixture(), state=saved, as_of=NOW)["catalogue"]
        self.assertEqual(value["state"], "partial")
        self.assertIsNone(value["totals"]["meta_discovery_not_dispatched"])

    def test_new_typed_record_does_not_invent_missing_paid_history(self):
        saved = state()
        saved["query_tasks"]["new-question"] = {"kind": "research-question-v2",
            "attempt_id": "not-a-legacy-paid-task"}
        value = programme_cost_capacity_summary(catalogue=catalogue_fixture(), state=saved, as_of=NOW)["catalogue"]
        self.assertEqual(value["state"], "known")
        self.assertEqual(value["totals"]["meta_discovery_not_dispatched"], 1)

    def test_original156_fit_acceptance_does_not_become_fresh_yield(self):
        original = ["original-" + str(n) for n in range(156)]
        frozen = record(original); saved = state()
        for cid in original + ["fresh-one", "fresh-two"]:
            add_review(saved, cid)
        frozen["follow_on_reviews"]["follow-on"] = {"concept_ids": ["fresh-one", "fresh-two"], "source_run_id": "fresh-source-run"}
        saved["batch_runs"]["actual-discovery"] = {"kind": "catalogue-discovery-cycle-v1", "programme_id": frozen["programme_id"],
            "new_concept_ids": ["fresh-one", "fresh-two"], "actions": {}}
        pool = {"eligible": [product(cid) for cid in original + ["fresh-one"]],
                "decisions": [{"concept_id": "fresh-one", "decision": "included"}, {"concept_id": "fresh-two", "decision": "deferred"}]}
        value = programme_cost_capacity_summary(frozen, state=saved, report_pool=pool, as_of=NOW)["research"]
        self.assertEqual(value["original_pool"]["fit_passing_products"], 156)
        self.assertEqual(value["discovery_new_distinct_concepts"], 2)
        self.assertEqual(value["currently_eligible_fresh_concepts"], 1)
        self.assertEqual(value["fresh_decisions_pending"], 1)
        self.assertIsNone(value["fresh_pool_ready_fraction"])

    def test_excluded_original_products_are_not_mislabelled_as_unfinished_reviews(self):
        original = ["original-" + str(n) for n in range(156)]
        saved = state()
        for cid in original:
            add_review(saved, cid)
        for n, status in enumerate(("excluded", "retired", "active")):
            concept = saved["concepts"][original[n]]
            concept["status"] = status
            concept["description"]["brand"] = "established"
        value = programme_cost_capacity_summary(record(original), state=saved, as_of=NOW)["research"]["original_pool"]
        self.assertEqual(value["products"], 156)
        self.assertEqual(value["deliberately_excluded_products"], 3)
        self.assertEqual(value["review_targets"], 153)
        self.assertEqual(value["current_practical_reviews"], 153)
        self.assertEqual(value["not_currently_reviewed"], 0)

    def test_partial_report_pool_does_not_inflate_completed_fresh_yield(self):
        frozen = record([]); saved = state()
        saved["batch_runs"]["actual-discovery"] = {"kind": "catalogue-discovery-cycle-v1", "programme_id": frozen["programme_id"],
            "new_concept_ids": ["fresh-one", "fresh-two"], "actions": {}}
        pool = {"eligible": [product("fresh-one"), product("fresh-two")],
                "decisions": [{"concept_id": "fresh-one", "decision": "included"}]}
        value = programme_cost_capacity_summary(frozen, state=saved, report_pool=pool, as_of=NOW)["research"]
        self.assertEqual(value["currently_eligible_fresh_concepts"], 2)
        self.assertEqual(value["fresh_completed_decision_eligibility_fraction"], "1.00000000")
        self.assertEqual(value["fresh_decisions_pending"], 1)
        self.assertIsNone(value["fresh_pool_ready_fraction"])

    def test_published_reserved_stale_and_foreign_stock_do_not_inflate_available_new(self):
        frozen = record(["new", "held", "stale", "used"]); frozen["reporting_inventory_concept_ids"] = ["carryover"]
        saved = state(); saved["published_history"]["publication"] = {"status": "published", "issue_id": "older-issue", "concept_id": "used", "client_id": "default"}
        saved["issue_reservations"]["hold"] = {"state": "reserved", "issue_id": "held-issue", "client_id": "default", "products": [{"concept_id": "held"}]}
        saved["issue_reservations"]["cancelled"] = {"state": "cancelled", "client_id": "default", "products": [{"concept_id": "new"}]}
        pool = {"as_of": NOW, "eligible": [product(cid) for cid in ("new", "held", "used", "carryover", "foreign")]
                + [product("stale", usable_until="2026-10-07")], "decisions": []}
        value = programme_cost_capacity_summary(frozen, state=saved, report_pool=pool, as_of=NOW)["report_supply"]
        self.assertEqual(value["eligible_scoped_products"], 5)
        self.assertEqual(value["eligible_unpublished_products"], 3)
        self.assertEqual(value["eligible_reserved_products"], 1)
        self.assertEqual(value["reserved_unpublished_products"], 1)
        self.assertEqual(value["available_unpublished_unreserved_products"], 2)
        self.assertEqual(value["eligible_previously_published_products"], 1)
        self.assertIsNone(value["complete_allocated_issues"])

    def test_actual_allocator_shortfalls_and_logical_issue_dedup_are_retained(self):
        products = [product("candidate-" + str(n)) for n in range(25)]
        frozen = record([row["concept_id"] for row in products]); saved = state()
        allocations = allocate_issues(products, frozen["release_slots"], {}, {})
        allocations["issues"].append(deepcopy(allocations["issues"][0]))  # A repeated edition is not another issue.
        value = programme_cost_capacity_summary(frozen, state=saved, report_pool={"eligible": products, "decisions": []},
            allocations=allocations, as_of=NOW)["report_supply"]
        self.assertEqual(value["complete_allocated_issues"], 2)
        self.assertEqual(value["incomplete_allocated_issues"], 1)
        self.assertEqual(value["allocated_distinct_products"], 25)
        self.assertEqual([row["shortfall"] for row in value["issue_shortfalls"]], [0, 0, 5])

    def test_conflicting_same_issue_allocations_do_not_hide_healthy_stock_counts(self):
        products = [product("candidate-" + str(n)) for n in range(25)]
        frozen = record([row["concept_id"] for row in products])
        allocations = allocate_issues(products, frozen["release_slots"], {}, {})
        another = deepcopy(allocations["issues"][0])
        another["products"][0] = products[-1]  # Same counts, different identity: not another language edition.
        allocations["issues"].append(another)
        value = programme_cost_capacity_summary(frozen, state=state(), report_pool={"eligible": products, "decisions": []},
            allocations=allocations, as_of=NOW)["report_supply"]
        self.assertEqual(value["state"], "known")
        self.assertEqual(value["available_unpublished_unreserved_products"], 25)
        self.assertEqual(value["allocation_state"], "unknown")
        self.assertIsNone(value["complete_allocated_issues"])

    def test_allocations_for_a_different_issue_do_not_claim_current_ready_reports(self):
        products = [product("candidate-" + str(n)) for n in range(10)]
        frozen = record([row["concept_id"] for row in products])
        allocations = allocate_issues(products, frozen["release_slots"], {}, {})
        allocations["issues"][0]["issue_id"] = "other-week-issue"
        value = programme_cost_capacity_summary(frozen, state=state(), report_pool={"eligible": products, "decisions": []},
            allocations=allocations, as_of=NOW)["report_supply"]
        self.assertEqual(value["available_unpublished_unreserved_products"], 10)
        self.assertEqual(value["allocation_state"], "unknown")
        self.assertIsNone(value["complete_allocated_issues"])

    def test_partial_allocations_do_not_erase_the_unreported_logical_slots(self):
        products = [product("candidate-" + str(n)) for n in range(25)]
        frozen = record([row["concept_id"] for row in products])
        original = allocate_issues(products, frozen["release_slots"], {}, {})
        for count in (0, 1, 2):
            with self.subTest(supplied_issues=count):
                allocations = deepcopy(original); allocations["issues"] = allocations["issues"][:count]
                value = programme_cost_capacity_summary(frozen, state=state(), financial_state=financial(),
                    report_pool={"eligible": products, "decisions": []}, allocations=allocations, as_of=NOW)
                self.assertEqual(value["money"]["state"], "known")
                self.assertEqual(value["report_supply"]["available_unpublished_unreserved_products"], 25)
                self.assertEqual(value["report_supply"]["logical_release_slots"], 3)
                self.assertEqual(value["report_supply"]["allocation_state"], "unknown")
                self.assertIsNone(value["report_supply"]["complete_allocated_issues"])
                self.assertIsNone(value["report_supply"]["incomplete_allocated_issues"])
                self.assertIsNone(value["report_supply"]["issue_shortfalls"])

    def test_wrong_or_missing_slot_client_and_date_leave_allocation_unknown(self):
        products = [product("candidate-" + str(n)) for n in range(10)]
        frozen = record([row["concept_id"] for row in products])
        original = allocate_issues(products, frozen["release_slots"], {}, {})
        for field, replacement in (("client_id", "another-client"), ("client_id", None),
                                   ("date", "2026-10-19"), ("date", None)):
            with self.subTest(field=field, replacement=replacement):
                allocations = deepcopy(original)
                if replacement is None:
                    del allocations["issues"][0][field]
                else:
                    allocations["issues"][0][field] = replacement
                value = programme_cost_capacity_summary(frozen, state=state(),
                    report_pool={"eligible": products, "decisions": []}, allocations=allocations, as_of=NOW)["report_supply"]
                self.assertEqual(value["available_unpublished_unreserved_products"], 10)
                self.assertEqual(value["allocation_state"], "unknown")
                self.assertIsNone(value["complete_allocated_issues"])

    def test_lower_yield_and_actual_declared_release_count_increase_cost(self):
        for reports in (12, 13, 14):
            with self.subTest(reports=reports):
                value = programme_cost_capacity_summary(forecast={"baseline_collection_usd": "0.50040006", "baseline_candidates": 156,
                    "new_products_per_report": 10, "report_count": reports}, as_of=NOW)["forecast"]
                planning, stress = value["scenarios"]
                self.assertEqual(planning["candidates_total"], 200 * reports)
                self.assertEqual(stress["candidates_total"], 334 * reports)
                self.assertGreater(Decimal(stress["collection_total_usd"]), Decimal(planning["collection_total_usd"]))
                self.assertIsNone(planning["all_in_estimate_usd"])
                self.assertTrue(value["allowance_fit_not_inferred"])

    def test_reduced_effort_is_used_without_receipt_scan_or_invented_invoice(self):
        effort = {"model": {"saved_job_attempts": 2, "usage_receipts": 1, "attempts_without_usable_token_counts": 1,
            "duration_receipts": 1, "recorded_tokens": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10},
            "recorded_duration_sum_seconds": 2.5}, "direct": {"recognised_acquisitions": 4, "actual_dispatches": 2,
            "proved_pre_dispatch_failures": 1, "uncertain_dispatches": 1, "states": {"blocked": 1, "empty": 1, "failed": 2}}}
        result = programme_cost_capacity_summary(effort_rows=effort, as_of=NOW)["effort"]
        self.assertEqual(result["state"], "known")
        self.assertEqual(result["model"]["recorded_tokens"]["input_tokens"], 100)
        self.assertEqual(result["direct"]["actual_dispatches"], 2)
        self.assertIsNone(result["incremental_ai_invoice_usd"])
        raw = programme_cost_capacity_summary(effort_rows={"direct": {"schema_version": 1, "route": "meta-ads"}}, as_of=NOW)
        self.assertEqual(raw["effort"]["state"], "unknown")

    def test_paid_capture_financial_references_alone_are_not_measured_computer_effort(self):
        value = programme_cost_capacity_summary(financial_state=financial(),
            effort_rows={"paid_attempt_ids": ["shared-attempt"]}, as_of=NOW)
        self.assertEqual(value["money"]["selected_supporting_cost_state"], "known")
        self.assertEqual(value["money"]["selected_supporting_acquisitions"], 1)
        self.assertEqual(Decimal(value["money"]["selected_known_incurred_usd"]), Decimal("0.5"))
        self.assertEqual(value["effort"]["state"], "unknown")
        self.assertIsNone(value["effort"]["model"])
        self.assertIsNone(value["effort"]["direct"])
        self.assertIsNone(value["effort"]["incremental_ai_invoice_usd"])
        self.assertEqual(value["effort"]["reason"], "SUMMARY_REDUCED_MODEL_AND_DIRECT_EFFORT_NOT_SUPPLIED")

    def test_one_measured_effort_lane_is_partial_and_explicit_zero_is_not_missing(self):
        model = {"saved_job_attempts": 0, "usage_receipts": 0, "attempts_without_usable_token_counts": 0,
            "duration_receipts": 0, "recorded_tokens": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0},
            "recorded_duration_sum_seconds": 0}
        direct = {"recognised_acquisitions": 0, "actual_dispatches": 0, "proved_pre_dispatch_failures": 0,
            "uncertain_dispatches": 0, "states": {}}
        for supplied in ({}, {"model": None, "direct": None}, {"paid_attempt_ids": []}):
            with self.subTest(supplied=supplied):
                self.assertEqual(programme_cost_capacity_summary(effort_rows=supplied, as_of=NOW)["effort"]["state"], "unknown")
        for supplied in ({"model": model}, {"direct": direct}, {"model": model, "direct": None}):
            with self.subTest(lane=list(supplied)):
                result = programme_cost_capacity_summary(effort_rows=supplied, as_of=NOW)["effort"]
                self.assertEqual(result["state"], "partial")
                self.assertNotIn("reason", result)
        result = programme_cost_capacity_summary(effort_rows={"model": model, "direct": direct}, as_of=NOW)["effort"]
        self.assertEqual(result["state"], "known")
        self.assertEqual(result["model"]["saved_job_attempts"], 0)
        self.assertEqual(result["direct"]["actual_dispatches"], 0)

    def test_inconsistent_reduced_effort_counts_are_unknown_not_fabricated_progress(self):
        for direct in (
            {"recognised_acquisitions": 4, "actual_dispatches": 4, "proved_pre_dispatch_failures": 1,
             "uncertain_dispatches": 0, "states": {"success": 4}},
            {"recognised_acquisitions": 4, "actual_dispatches": 2, "proved_pre_dispatch_failures": 1,
             "uncertain_dispatches": 1, "states": {"success": 5}},
        ):
            with self.subTest(direct=direct):
                value = programme_cost_capacity_summary(effort_rows={"direct": direct}, as_of=NOW)["effort"]
                self.assertEqual(value["state"], "unknown")

    def test_invalid_record_input_does_not_suppress_healthy_money(self):
        value = programme_cost_capacity_summary([], financial_state=financial(), as_of=NOW)
        self.assertEqual(value["research"]["state"], "unknown")
        self.assertEqual(value["report_supply"]["state"], "unknown")
        self.assertEqual(value["money"]["state"], "known")

    def test_summary_is_pure_does_not_scan_stores_and_omits_private_money_identifiers(self):
        frozen = record(["original"]); saved = state(); add_review(saved, "original")
        pool = {"as_of": NOW, "eligible": [product("original")], "decisions": [{"concept_id": "original", "decision": "included"}]}
        arguments = {"record": frozen, "state": saved, "financial_state": financial(), "account_status": account_status(),
                     "catalogue": catalogue_fixture(), "report_pool": pool, "as_of": NOW}
        original = deepcopy(arguments)
        with patch("builtins.open", side_effect=AssertionError("File read forbidden")), \
             patch.object(Path, "glob", side_effect=AssertionError("Scan forbidden")), \
             patch.object(Path, "rglob", side_effect=AssertionError("Scan forbidden")), \
             patch("product_radar.storage.Journal.state", side_effect=AssertionError("Ledger reread forbidden")), \
             patch("product_radar.research_memory.ResearchMemory.snapshot", side_effect=AssertionError("Snapshot reread forbidden")):
            result = programme_cost_capacity_summary(**arguments)
            self.assertEqual(result, programme_cost_capacity_summary(**arguments))
        self.assertEqual(arguments, original)
        self.assertNotIn(PRIVATE, json.dumps(result))
        self.assertEqual(result["research"]["original_pool"]["current_practical_reviews"], 1)
        self.assertEqual(result["money"]["state"], "known")
        self.assertFalse(result["client_publication_attempted"])

    def test_cost_snapshot_connects_calendar_and_shared_average_not_exclusive_bill(self):
        from test_programme_cost_snapshot import ROOT, baseline, draft
        from product_radar.service import load_policy
        frozen = record([]); frozen["drafts"] = {"issue-0": draft("issue-0"), "issue-1": draft("issue-1")}
        saved, ledger = state(), financial()
        snapshot = build_owner_cost_snapshot(frozen, saved, ledger, baseline=baseline(), policy=load_policy(),
            account_status=account_status(), as_of=NOW, project_root=ROOT)
        original = deepcopy(snapshot)
        value = programme_cost_capacity_summary(frozen, state=saved, financial_state=ledger,
            cost_snapshot=snapshot, as_of=NOW)
        self.assertEqual(value["forecast"]["calendar_issue_count"], 14)
        self.assertEqual(value["forecast"]["scenarios"][0]["candidates_total"], 2800)
        self.assertEqual(value["generated_reports"]["complete_generated_logical_issues"], 2)
        self.assertEqual(value["shared_project_cost_average"]["known_shared_average_usd"], "0.25400000")
        self.assertIsNone(value["per_issue_cash_cost_usd"])
        self.assertIsNone(value["money"]["per_issue_cost_usd"])
        self.assertNotIn(PRIVATE, json.dumps(value))
        self.assertEqual(snapshot, original)

    def test_current_allocation_proposal_and_saved_generated_reports_are_separate(self):
        from test_programme_cost_snapshot import ROOT, draft
        products = [product("candidate-" + str(n)) for n in range(25)]
        frozen = record([row["concept_id"] for row in products])
        frozen["drafts"] = {"issue-0": draft("issue-0")}
        allocations = allocate_issues(products, frozen["release_slots"], {}, {})
        value = programme_cost_capacity_summary(frozen, state=state(), report_pool={"eligible": products, "decisions": []},
            allocations=allocations, allocation_basis="current_research_proposal", project_root=ROOT, as_of=NOW)
        self.assertEqual(value["report_supply"]["allocation_basis"], "current_research_proposal")
        self.assertEqual(value["report_supply"]["complete_allocated_issues"], 2)
        self.assertEqual(value["generated_reports"]["complete_generated_logical_issues"], 1)
        self.assertIn("not generated files", value["report_supply"]["note"])
        plain = programme_cost_capacity_summary(frozen, state=state(), report_pool={"eligible": products, "decisions": []},
            allocations=allocations, as_of=NOW)
        self.assertEqual(plain["report_supply"]["allocation_basis"], "supplied_plan")

    def test_old_explicit_forecast_remains_supported_and_wins_without_changed_unit_cost(self):
        from test_programme_cost_snapshot import ROOT, baseline, draft
        from product_radar.service import load_policy
        frozen = record([]); frozen["drafts"] = {"issue-0": draft("issue-0")}
        snapshot = build_owner_cost_snapshot(frozen, state(), financial(), baseline=baseline(), policy=load_policy(),
            as_of=NOW, project_root=ROOT)
        explicit = {"baseline_collection_usd": "0.50040006", "baseline_candidates": 156,
                    "new_products_per_report": 10, "report_count": 12}
        value = programme_cost_capacity_summary(frozen, financial_state=financial(), cost_snapshot=snapshot,
                                               forecast=explicit, as_of=NOW)
        self.assertEqual(value["forecast"]["report_count"], 12)
        self.assertEqual(value["shared_project_cost_average"]["known_shared_average_usd"], "0.50800000")

    def test_stale_or_forged_average_snapshot_does_not_hide_healthy_money(self):
        from test_programme_cost_snapshot import ROOT, baseline, draft
        from product_radar.service import load_policy
        frozen = record([]); frozen["drafts"] = {"issue-0": draft("issue-0")}
        original = build_owner_cost_snapshot(frozen, state(), financial(), baseline=baseline(), policy=load_policy(),
            as_of=NOW, project_root=ROOT)
        for field, replacement in (("known_shared_average_usd", "0.01"), ("known_project_collection_usd", "NaN"),
                                   ("exclusive_per_issue_cash_cost_usd", "0.5")):
            with self.subTest(field=field):
                snapshot = deepcopy(original); snapshot["shared_project_cost_average"][field] = replacement
                value = programme_cost_capacity_summary(frozen, financial_state=financial(), cost_snapshot=snapshot, as_of=NOW)
                self.assertEqual(value["shared_project_cost_average"]["state"], "unknown")
                self.assertEqual(value["money"]["state"], "known")
                self.assertEqual(Decimal(value["money"]["ledger_known_incurred_usd"]), Decimal("0.508"))
        value = programme_cost_capacity_summary(frozen, financial_state=financial(), cost_snapshot=original,
                                               as_of="2026-10-08T11:00:00+00:00")
        self.assertEqual(value["shared_project_cost_average"]["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
