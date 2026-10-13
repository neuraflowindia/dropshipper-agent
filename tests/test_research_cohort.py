"""Actual synthetic waves, original ownership and honest owner cost scenarios."""

from copy import deepcopy
from decimal import Decimal, ROUND_CEILING
import unittest
from unittest.mock import patch

from product_radar.catalogue_coverage import catalogue_matrix
from product_radar.catalogue_discovery import prepare_next_discovery_wave
from product_radar.models import SafetyError, digest, timestamp
from product_radar.operational_effort_cache import refresh_model_effort_checkpoint
from product_radar.programme_cost_snapshot import build_owner_cost_snapshot
from product_radar.programme_summary import programme_cost_capacity_summary
from product_radar.research_cohort import (current_cohort_forecast, current_research_cohort,
                                         discovery_counts, discovery_questions, _original_costs)
from product_radar.service import load_policy
from product_radar.source_acquisitions import (claim_source_acquisition, mark_source_dispatching,
                                              mark_source_execution_unknown, record_source_response)
import test_discovery_waves as wave_fixtures
import test_source_acquisitions as acquisition_fixtures
from test_programme_summary import record, product, financial
from test_research_flow import FakeRunner

NOW = wave_fixtures.NOW


class ResearchCohortTests(unittest.TestCase):
    def setUp(self):
        wave_fixtures.DiscoveryWaveTests.setUp(self)
        self.record = record([]); self.record["programme_id"] = "programme"
        self.pool = {"as_of": NOW, "eligible": [], "decisions": []}

    handlers = wave_fixtures.DiscoveryWaveTests.handlers
    no_admission = wave_fixtures.DiscoveryWaveTests.no_admission
    advance = wave_fixtures.DiscoveryWaveTests.advance
    waves = wave_fixtures.DiscoveryWaveTests.waves

    def ledger(self):
        with self.memory.journal.locked(): return self.memory.journal.state()

    def cohort(self, **kwargs):
        return current_research_cohort(self.record, self.memory.snapshot(), self.ledger(), self.pool,
                                      as_of=NOW, **kwargs)

    def collect(self):
        self.mode["amazon-search"] = "usable"
        with patch("product_radar.research_ai.utc_now", return_value=NOW):
            result = self.advance(max_actions=3, runner=FakeRunner(), admission=None)
        self.cid = result["new_concept_ids"][0]
        self.record["follow_on_reviews"] = {"child": {"concept_ids": [self.cid], "source_run_id": "follow-source"}}
        self.pool["eligible"] = [product(self.cid)]
        self.pool["decisions"] = [{"concept_id": self.cid, "decision": "included"}]
        return result

    def test_prepared_questions_are_not_started_searches(self):
        prepare_next_discovery_wave(self.memory, "programme", self.catalogue, handlers=self.handlers(), include_lists=False, as_of=NOW)
        rows = discovery_questions(self.memory.snapshot())
        self.assertEqual(len(rows), 3)
        counts = discovery_counts(rows)
        self.assertEqual(counts["prepared_questions"], 3)
        self.assertEqual(counts["physical_discovery_reads"], 0)
        self.assertEqual(self.calls, [])

    def test_actual_two_callbacks_are_not_zero_and_same_original_counts_once(self):
        self.advance(max_actions=3)
        saved = self.memory.snapshot(); rows = discovery_questions(saved)
        self.assertEqual(len(self.calls), 2)
        counts = discovery_counts(rows + rows)
        self.assertEqual(counts["physical_discovery_reads"], 2)
        self.assertEqual(counts["prepared_questions"], 3)
        matrix = catalogue_matrix(self.catalogue, saved)
        self.assertEqual(matrix["totals"]["actual_public_discovery_queries"], 2)
        self.assertFalse(any(row["kind"] == "query_task" and row["id"] in {r["question_id"] for r in rows}
                             for row in matrix["unmapped"]))

    def test_actual_v2_new_concept_enters_owner_counts(self):
        self.collect()
        value = programme_cost_capacity_summary(self.record, state=self.memory.snapshot(), report_pool=self.pool,
                                                as_of=NOW)["research"]
        self.assertEqual(value["discovery_waves"], 1)
        self.assertEqual(value["discovery_new_distinct_concepts"], 1)
        self.assertEqual(value["discovery_unique_source_items"], 1)
        self.assertEqual(value["direct_discovery"]["physical_discovery_reads"], 2)

    def test_fresh_known_zero_collection_is_separate_from_unknown_AI_invoice(self):
        self.collect(); before = self.memory.snapshot()
        checkpoint = refresh_model_effort_checkpoint(self.memory, before, as_of=NOW)
        result = self.cohort(model_effort=checkpoint["model"])
        self.assertEqual(result["state"], "known")
        self.assertEqual(result["unique_source_candidates"], 1)
        self.assertEqual(result["never_shown_unreserved_new_types"], 1)
        self.assertEqual(Decimal(result["measured_current_useful_new_yield"]), Decimal(1))
        self.assertEqual(result["costs"]["provider_collection_usd"], "0")
        self.assertEqual(result["costs"]["known_zero_originals"], 2)
        self.assertGreater(result["model"]["saved_response_receipts"], 0)
        self.assertGreater(result["model"]["recorded_tokens"]["input_tokens"], 0)
        self.assertIsNone(result["model"]["incremental_invoice_usd"])
        self.assertEqual(before, self.memory.snapshot())

    def test_unfinished_real_capture_keeps_yield_and_cash_forecast_unknown(self):
        self.mode["amazon-search"] = "usable"; self.advance(max_actions=3)
        value = self.cohort()
        self.assertEqual(value["state"], "insufficient_sample")
        self.assertIn("COHORT_ASSESSMENT_OR_IDENTITY_UNFINISHED", value["reasons"])
        self.assertIsNone(value["measured_current_useful_new_yield"])
        cases = current_cohort_forecast(value, load_policy(), as_of=NOW)
        self.assertEqual(cases["state"], "insufficient_sample")
        self.assertTrue(all(row["provider_collection_estimate_usd"] is None for row in cases["scenarios"]))

    def test_saved_original_reuse_does_not_become_another_physical_search(self):
        self.advance(max_actions=3)
        rows = discovery_questions(self.memory.snapshot())
        second = deepcopy(rows[0]); second["question_id"] = "another-semantic-question"
        second["action"]["reused_acquisition"] = True
        counts = discovery_counts(rows + [second])
        self.assertEqual(counts["physical_discovery_reads"], 2)
        self.assertEqual(counts["questions_reusing_original_reads"], 1)

    def test_new_uncertain_original_is_not_hidden_by_earlier_answer_to_same_question(self):
        self.advance(max_actions=3); rows = discovery_questions(self.memory.snapshot())
        earlier = next(row for row in rows if row["action"].get("dispatch_started") is True)
        later = deepcopy(earlier); later["cycle_id"] = "later-renewal-wave"
        later["action"] = {"state": "execution_uncertain", "acquisition_id": "later-original", "dispatch_started": False}
        counts = discovery_counts(rows + [later])
        self.assertEqual(counts["physical_discovery_reads"], 2)
        self.assertEqual(counts["uncertain_questions"], 1)
        self.assertEqual(counts["uncertain_originals"], 1)

    def test_proved_started_original_with_lost_reply_is_both_attempted_and_uncertain(self):
        self.advance(max_actions=3); rows = discovery_questions(self.memory.snapshot())
        row = deepcopy(next(row for row in rows if row["action"].get("dispatch_started") is True))
        row["action"]["state"] = "execution_uncertain"
        counts = discovery_counts([row, row])
        self.assertEqual(counts["attempted_questions"], 1)
        self.assertEqual(counts["physical_discovery_reads"], 1)
        self.assertEqual(counts["answered_questions"], 0)
        self.assertEqual(counts["uncertain_questions"], 1)
        self.assertEqual(counts["uncertain_originals"], 1)

    def test_uncertain_original_is_not_free_or_a_new_cost_baseline(self):
        self.mode["amazon-search"] = TimeoutError("Synthetic no confirmed reply")
        self.advance(max_actions=3)
        value = self.cohort()
        self.assertEqual(value["state"], "insufficient_sample")
        self.assertGreater(value["costs"]["pending_originals"], 0)
        self.assertIsNone(value["costs"]["provider_collection_usd"])

    def test_owner_reserved_earlier_but_actually_collected_now_is_current_not_saved_reuse(self):
        wave = prepare_next_discovery_wave(self.memory, "programme", self.catalogue,
            handlers=self.handlers(), include_lists=False, as_of=NOW)
        question = next(q for action in wave["planned_actions"] for q in action["questions"]
                        if q["route"] == "amazon-search")
        owner = claim_source_acquisition(self.memory, question["route"], question["request"],
            owner_id=question["id"], as_of="2026-10-08T10:00:00+00:00",
            collection_channel="direct_local", incremental_collection_fee="known_zero",
            artifact_roots=[str(self.memory.root / "direct_marketplace")])
        self.collect(); value = self.cohort()
        self.assertEqual(value["state"], "known")
        self.assertEqual(value["unique_source_candidates"], 1)
        self.assertEqual(value["costs"]["known_zero_originals"], 2)
        self.assertEqual(value["costs"]["saved_older_originals_reused"], 0)
        self.assertIn(owner["acquisition_id"], {aid for aid, _ in self.calls})

    def test_older_uncertain_origin_without_original_response_is_not_free_reuse(self):
        owner = claim_source_acquisition(self.memory, "amazon-search",
            {"query": "synthetic older uncertain", "country": "IN", "limit": 30}, owner_id="older-question",
            as_of="2026-10-08T10:00:00+00:00", artifact_roots=[str(self.memory.root)])
        mark_source_dispatching(self.memory, owner["acquisition_id"], as_of="2026-10-08T10:01:00+00:00")
        mark_source_execution_unknown(self.memory, owner["acquisition_id"],
            reason="Synthetic original response not present.", as_of="2026-10-08T10:02:00+00:00")
        value = _original_costs(self.memory.snapshot(), [owner["acquisition_id"]], self.ledger(),
                                started_at=timestamp(NOW), as_of=timestamp(NOW))
        self.assertEqual(value["saved_older_originals_reused"], 0)
        self.assertEqual(value["pending_originals"], 1)
        self.assertIsNone(value["provider_collection_usd"])

    def test_full_scenario_matrix_uses_backlog_once_and_rounds_each_report_up(self):
        self.collect(); value = self.cohort()
        value["stock"]["available_new_types"] = 11
        value["cohort_hash"] = digest({k: v for k, v in value.items() if k != "cohort_hash"})
        cases = current_cohort_forecast(value, load_policy(), as_of=NOW)
        self.assertEqual(len(cases["scenarios"]), 8)
        self.assertEqual({row["entries_per_report"] for row in cases["scenarios"]}, {10, 15})
        for row in cases["scenarios"]:
            demand = row["new_types_per_report"] * cases["calendar_issue_count"]
            self.assertEqual(row["new_types_needed_after_backlog"], max(0, demand - 11))
            backlog, expected = 11, 0
            for _ in range(cases["calendar_issue_count"]):
                used = min(backlog, row["new_types_per_report"]); backlog -= used
                expected += int((Decimal(row["new_types_per_report"] - used) / Decimal(row["assumed_useful_new_yield"])).to_integral_value(rounding=ROUND_CEILING))
            self.assertEqual(row["new_candidates_needed_after_backlog"], expected)
            self.assertIsNone(row["all_in_estimate_usd"])
            self.assertFalse(row["local_runtime_cash_limit_applied"])

    def test_same_day_expired_support_is_not_live_backlog(self):
        self.collect(); self.pool["eligible"][0]["usable_until"] = "2026-10-09T09:59:59+00:00"
        self.assertEqual(self.cohort()["stock"]["available_new_types"], 0)

    def test_unrelated_global_pool_stock_is_not_this_programmes_backlog(self):
        self.collect(); self.pool["eligible"].append(product("other-programme-type"))
        self.assertEqual(self.cohort()["stock"]["available_new_types"], 1)

    def test_reserving_useful_new_stock_does_not_erase_its_discovery_yield(self):
        self.collect(); before = self.cohort()
        self.memory.put("issue_reservations", "hold", {"issue_id": "held-issue", "client_id": "default",
            "state": "reserved", "products": [{"concept_id": self.cid}]}, event_id="synthetic-cohort-hold")
        after = self.cohort()
        self.assertEqual(before["measured_current_useful_new_yield"], after["measured_current_useful_new_yield"])
        self.assertEqual(after["useful_new_types_observed"], 1)
        self.assertEqual(after["never_shown_unreserved_new_types"], 0)
        self.assertEqual(after["stock"]["available_new_types"], 0)

    def test_actual_publication_is_productive_but_not_new_stock_or_permanent_hold(self):
        self.collect()
        from product_radar.issue_allocation import _publication_changes
        issue = {"issue_id": "older-issue", "client_id": "default", "revision": 1, "date": "2026-10-09",
                 "projected_ordinal": 1, "evidence_fingerprint": digest("synthetic original accepted facts"),
                 "products": [product(self.cid)]}
        self.memory.commit("synthetic-cohort-publication", _publication_changes(self.memory.snapshot(), issue,
            {"synthetic_activation_receipt": True}, timestamp(NOW)))
        self.memory.put("issue_reservations", "published-hold", {"issue_id": "older-issue", "client_id": "default",
            "state": "published", "products": [{"concept_id": self.cid}]}, event_id="synthetic-cohort-published-hold")
        self.pool["eligible"] = []; self.pool["decisions"] = [{"concept_id": self.cid, "decision": "excluded"}]
        value = self.cohort()
        self.assertEqual(Decimal(value["measured_current_useful_new_yield"]), 1)
        self.assertEqual(value["stock"]["available_new_types"], 0)
        self.pool["decisions"] = [{"concept_id": self.cid, "decision": "deferred", "codes": ["useful_update_review_required"]}]
        pending_update = self.cohort()
        self.assertEqual(pending_update["state"], "known")
        self.assertEqual(Decimal(pending_update["measured_current_useful_new_yield"]), 1)
        self.pool["eligible"] = [product(self.cid, status="Updated")]
        self.assertEqual(self.cohort()["stock"]["accepted_available_updates"], 1)
        self.memory.put("issue_reservations", "unreconciled", {"issue_id": "unreconciled-issue", "client_id": "default",
            "state": "published", "products": [{"concept_id": self.cid}]}, event_id="synthetic-cohort-unreconciled-hold")
        self.assertEqual(self.cohort()["stock"]["accepted_available_updates"], 0)

    def test_reservations_without_original_model_measurements_are_not_calls_or_tokens(self):
        self.collect(); model = self.cohort()["model"]
        self.assertEqual(model["admitted_job_attempts"], 2)
        self.assertEqual(model["actual_execution_receipts"], 0)
        self.assertEqual(model["saved_response_receipts"], 0)
        self.assertEqual(model["recorded_tokens"]["input_tokens"], 0)
        self.assertEqual(model["attempts_without_current_measurement"], 2)

    def test_failed_paid_original_and_unsettled_hold_are_counted_once(self):
        self.route = "amazon-search"; self.request = {"query": "synthetic paid sink caddy", "country": "IN", "limit": 30}
        ids = []
        for attempt in ("failed-attempt", "shared-attempt"):
            self.request = {**self.request, "query": "synthetic " + attempt}
            owner = claim_source_acquisition(self.memory, self.route, self.request, owner_id="question-" + attempt,
                original_attempt_id=attempt, as_of=NOW, collection_channel="funded_provider",
                incremental_collection_fee="reserved_paid", origin_account_key="synthetic-account",
                artifact_roots=[str(self.memory.root)])
            receipt = acquisition_fixtures.SourceAcquisitionTests.save_receipt(self, owner, state="failed")
            record_source_response(self.memory, owner["acquisition_id"], receipt_path=receipt, as_of=NOW)
            ids.append(owner["acquisition_id"])
        costs = _original_costs(self.memory.snapshot(), ids + ids, financial(),
                                started_at=timestamp(NOW), as_of=timestamp(NOW))
        self.assertEqual(costs["paid_attempts"], 2)
        self.assertEqual(costs["current_originals"], 2)
        self.assertEqual(Decimal(costs["known_provider_collection_usd"]), Decimal("0.508"))
        self.assertEqual(Decimal(costs["unsettled_provider_holds_usd"]), Decimal("0.02"))
        self.assertIsNone(costs["provider_collection_usd"])
        self.assertEqual(costs["direct_collection"]["proved_known_zero_originals"], 0)

    def test_proposed_zero_fee_owner_without_actual_expense_proof_stays_unpriced(self):
        self.route = "amazon-search"; self.request = {"query": "synthetic unpriced sink caddy", "country": "IN", "limit": 30}
        owner = claim_source_acquisition(self.memory, self.route, self.request, owner_id="unknown-fee-question",
            as_of=NOW, collection_channel="direct_local", incremental_collection_fee="known_zero",
            artifact_roots=[str(self.memory.root)])
        receipt = acquisition_fixtures.SourceAcquisitionTests.save_receipt(self, owner)
        record_source_response(self.memory, owner["acquisition_id"], receipt_path=receipt, as_of=NOW)
        costs = _original_costs(self.memory.snapshot(), [owner["acquisition_id"]], self.ledger(),
                                started_at=timestamp(NOW), as_of=timestamp(NOW))
        self.assertEqual(costs["known_zero_originals"], 0)
        self.assertEqual(costs["unknown_fee_originals"], 1)
        self.assertIsNone(costs["provider_collection_usd"])

    def test_forecast_refuses_resealed_invalid_counts_and_unbound_known_sample(self):
        self.collect(); value = self.cohort()
        value["unique_source_candidates"] += 1
        with self.assertRaisesRegex(SafetyError, "CURRENT_COHORT_BINDING_INVALID"):
            current_cohort_forecast(value, load_policy(), as_of=NOW)
        value = self.cohort(); value["stock"]["available_new_types"] = -1
        value["cohort_hash"] = digest({key: row for key, row in value.items() if key != "cohort_hash"})
        with self.assertRaisesRegex(SafetyError, "CURRENT_COHORT_STOCK_INVALID"):
            current_cohort_forecast(value, load_policy(), as_of=NOW)

    def test_no_current_waves_does_not_promote_the_historical_paid_sample(self):
        from test_programme_cost_snapshot import baseline
        value = build_owner_cost_snapshot(self.record, self.memory.snapshot(), financial(), baseline=baseline(),
                                         policy=load_policy(), as_of=NOW, report_pool=self.pool)
        self.assertEqual(value["conditional_forecast"]["state"], "known")
        self.assertEqual(value["current_research_cohort"]["state"], "insufficient_sample")
        self.assertEqual(value["current_forecast"]["state"], "insufficient_sample")
        self.assertTrue(value["historical_forecast_is_not_current_source_cost"])

    def test_current_cohort_and_forecast_reach_existing_snapshot_and_summary(self):
        self.collect(); saved = self.memory.snapshot()
        snapshot = build_owner_cost_snapshot(self.record, saved, self.ledger(), policy=load_policy(),
                                            as_of=NOW, report_pool=self.pool)
        summary = programme_cost_capacity_summary(self.record, state=saved, financial_state=self.ledger(),
                                                  report_pool=self.pool, cost_snapshot=snapshot, as_of=NOW)
        self.assertEqual(summary["current_research_cohort"], snapshot["current_research_cohort"])
        self.assertEqual(summary["current_forecast"]["version"], "current-cohort-forecast-v1")
        self.assertEqual(summary["current_forecast"]["state"], "known")

