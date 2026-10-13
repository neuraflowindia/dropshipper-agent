"""Owner proposals use the current report format, not compatibility stock.

All eligibility comes from real disposable research writers and report-pool
readers. The synthetic runners only create fixture data; these owner reads
cannot contact a website/model, change research, or generate report files.
"""

from copy import deepcopy
from functools import partial
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import test_functional_source_market as functional_fixtures
import test_programme_reports as legacy_fixtures
from test_catalogue_expansion import fixture as catalogue_fixture
from test_programme_cost_snapshot import baseline
from test_programme_summary import account_status, financial
from test_report_source_facts import source_fixture
from product_radar.issue_allocation import allocate_issues
from product_radar.models import SafetyError, digest
from product_radar.operations import operation_run
from product_radar.programme import _reporting_eligible, prepare_programme, programme_owner_summary
from product_radar.report_examples import RECOMMENDATION_CONTRACT
from product_radar.report_selection import build_report_pool
from product_radar.research_memory import ResearchMemory, change
from product_radar.service import load_policy
from product_radar.storage import Journal, atomic_json

NOW = legacy_fixtures.NOW


class ProgrammeOwnerFunctionalSummaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        # The reusable fixture normally requests ten discovery/fit subjects.
        # Here the same real writers receive twenty, so two disjoint proposals
        # have genuine current input; no eligibility validator is substituted.
        with patch.object(functional_fixtures, "run_flow",
                          wraps=partial(functional_fixtures.run_flow, limit=20)), \
             patch.object(functional_fixtures, "run_fit_reviews",
                          wraps=partial(functional_fixtures.run_fit_reviews, limit=20)):
            memory, cls.functional_ids = functional_fixtures.functional_pool_fixture(
                Path(temporary.name), count=20, at=NOW)
        cls.functional_state = memory.snapshot()
        pool = build_report_pool(memory, as_of=NOW,
            recommendation_contract=RECOMMENDATION_CONTRACT)
        if len(pool["eligible"]) != 20 or any(
                row.get("recommendation_contract") != RECOMMENDATION_CONTRACT
                for row in pool["eligible"]):
            raise AssertionError("Expected twenty real eligible synthetic functional types.")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"),
                                     evidence_type="synthetic")
        self.policy = load_policy()

    def seed(self, *, legacy_count=26, functional_count=0):
        saved = source_fixture(legacy_count).state
        for row in saved["assessments"].values():
            row["contract_version"] = "research-e-fit10-v2"
        self.memory.commit("owner-summary-synthetic-legacy-inputs", [change(table, ref, row)
            for table, rows in saved.items() for ref, row in rows.items()])
        if functional_count:
            self.memory.commit("owner-summary-synthetic-functional-inputs", [change(table, ref, row)
                for table, rows in self.functional_state.items() for ref, row in rows.items()])
        ids = sorted(saved["concepts"]) + self.functional_ids[:functional_count]
        self.record = prepare_programme(self.memory, "owner-functional-summary",
            source_run_id="owner-functional-sources", as_of=NOW, concept_ids=ids)
        self.state = self.memory.snapshot()
        self.pool = build_report_pool(self.memory, as_of=NOW,
            recommendation_contract=RECOMMENDATION_CONTRACT)
        scoped = _reporting_eligible(self.record, self.state, self.pool)
        self.assertEqual(len(scoped), legacy_count + functional_count)
        self.assertEqual(sum(row.get("recommendation_contract") == RECOMMENDATION_CONTRACT
                             for row in scoped), functional_count)
        return self.record

    def summary(self, **overrides):
        values = {"root": self.root, "state": self.state, "as_of": NOW,
            "financial_state": financial(), "account_status": account_status(),
            "catalogue": catalogue_fixture(), "policy": self.policy,
            "effort_rows": {}, "cost_baseline": baseline()}
        values.update(overrides)
        saved = deepcopy(self.state); record = deepcopy(self.record)
        pool = deepcopy(values.get("report_pool"))
        outputs = self.root / "data/outputs"
        original_files = {path.relative_to(outputs): path.read_bytes()
            for path in outputs.rglob("*") if path.is_file()}
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("No research reread")), \
             patch("urllib.request.urlopen", side_effect=AssertionError("No provider request")), \
             patch("product_radar.research_ai.CodexRunner.__call__",
                   side_effect=AssertionError("No real model request")):
            result = programme_owner_summary(self.memory, self.record, **values)
        self.assertEqual(self.state, saved)
        self.assertEqual(self.memory.snapshot(), saved)
        self.assertEqual(self.record, record)
        self.assertEqual(values.get("report_pool"), pool)
        self.assertEqual({path.relative_to(outputs): path.read_bytes()
            for path in outputs.rglob("*") if path.is_file()}, original_files)
        return result

    def assert_current(self, result, *, functional, legacy, complete):
        supply = result["report_supply"]
        self.assertEqual(supply["allocation_basis"], "current_research_proposal")
        self.assertEqual(supply["current_proposal_recommendation_contract"], RECOMMENDATION_CONTRACT)
        self.assertEqual(supply["eligible_scoped_products"], functional)
        self.assertEqual(supply["legacy_compatible_eligible_scoped_products"], legacy)
        self.assertEqual(supply["complete_allocated_issues"], complete)
        self.assertEqual(result["current_research_cohort"]["stock"]["available_new_types"], functional)
        self.assertEqual(result["generated_reports"]["complete_generated_logical_issues"], 0)
        return supply

    def test_twenty_six_legacy_rows_do_not_make_two_current_functional_proposals(self):
        self.seed()
        value = self.summary()
        supply = self.assert_current(value, functional=0, legacy=26, complete=0)
        self.assertEqual(supply["available_unpublished_unreserved_products"], 0)
        self.assertEqual(supply["allocated_distinct_products"], 0)
        self.assertEqual([row["shortfall"] for row in supply["issue_shortfalls"]], [10, 10, 10])
        # Compatibility remains a real, usable legacy pool, not retagged rows.
        compatible = allocate_issues(self.pool["eligible"], self.record["release_slots"],
            self.state["published_history"], self.state["issue_reservations"],
            policy=self.policy, identity_state=self.state)
        self.assertEqual(sum(row["ready_for_review"] for row in compatible["issues"]), 2)
        self.assertTrue(all(row.get("recommendation_contract") is None for row in self.pool["eligible"]))

    def test_one_current_type_is_not_padded_with_twenty_six_legacy_rows(self):
        self.seed(functional_count=1)
        value = self.summary(report_pool=self.pool)
        supply = self.assert_current(value, functional=1, legacy=26, complete=0)
        self.assertEqual(supply["allocated_distinct_products"], 1)
        # The first slot released before the functional fixture was observed.
        # Keep the real date/freshness rule; do not backdate new research.
        self.assertEqual([row["products"] for row in supply["issue_shortfalls"]], [0, 1, 0])
        self.assertEqual([row["shortfall"] for row in supply["issue_shortfalls"]], [10, 9, 10])

    def test_twenty_real_current_types_fill_two_disjoint_proposals_without_legacy_padding(self):
        self.seed(functional_count=20)
        selected = []
        def allocate(eligible, *args, **kwargs):
            selected.append(deepcopy(eligible))
            self.assertIs(kwargs["policy"], self.policy)
            return allocate_issues(eligible, *args, **kwargs)
        with patch("product_radar.issue_allocation.allocate_issues", side_effect=allocate):
            value = self.summary()
        supply = self.assert_current(value, functional=20, legacy=26, complete=2)
        self.assertEqual(supply["allocated_distinct_products"], 20)
        self.assertEqual([row["products"] for row in supply["issue_shortfalls"]], [0, 10, 10])
        self.assertEqual(len(selected), 1)
        self.assertEqual({row["concept_id"] for row in selected[0]}, set(self.functional_ids))
        self.assertTrue(all(row["recommendation_contract"] == RECOMMENDATION_CONTRACT
                            for row in selected[0]))

    def test_explicit_legacy_plan_remains_a_supplied_plan_not_a_current_format_claim(self):
        self.seed()
        allocations = allocate_issues(self.pool["eligible"], self.record["release_slots"],
            self.state["published_history"], self.state["issue_reservations"],
            policy=self.policy, identity_state=self.state)
        before = deepcopy(allocations)
        with patch("product_radar.issue_allocation.allocate_issues",
                   side_effect=AssertionError("Do not replace the supplied plan")):
            value = self.summary(report_pool=self.pool, allocations=allocations)
        supply = value["report_supply"]
        self.assertEqual(supply["allocation_basis"], "supplied_plan")
        self.assertIsNone(supply["current_proposal_recommendation_contract"])
        self.assertEqual(supply["legacy_compatible_eligible_scoped_products"], 26)
        self.assertEqual(supply["eligible_scoped_products"], 26)
        self.assertEqual(supply["complete_allocated_issues"], 2)
        self.assertEqual(value["current_research_cohort"]["stock"]["available_new_types"], 26)
        self.assertEqual(allocations, before)
        self.assertEqual(value["generated_reports"]["complete_generated_logical_issues"], 0)

    def test_missing_optional_allocation_keeps_current_stock_separate_from_legacy_stock(self):
        self.seed(functional_count=1)
        with patch("product_radar.issue_allocation.allocate_issues", side_effect=SafetyError("ISSUE_POOL_INVALID")):
            value = self.summary(report_pool=self.pool)
        supply = value["report_supply"]
        self.assertEqual(supply["eligible_scoped_products"], 1)
        self.assertEqual(supply["legacy_compatible_eligible_scoped_products"], 26)
        self.assertEqual(supply["available_unpublished_unreserved_products"], 1)
        self.assertIsNone(supply["complete_allocated_issues"])
        self.assertEqual(value["current_research_cohort"]["stock"]["available_new_types"], 1)
        self.assertEqual(value["optional_input_failures"], {"allocations": "ISSUE_POOL_INVALID"})

    def test_supplied_verified_state_needs_no_reread_or_useful_progress_credit(self):
        self.seed()
        with operation_run(self.root) as recorder:
            before = deepcopy(recorder.counts)
            value = self.summary()
            self.assertEqual(recorder.counts, before)
            self.assertIsNone(recorder.last_useful)
        self.assert_current(value, functional=0, legacy=26, complete=0)

    def test_completed_and_retained_legacy_packets_keep_the_historical_cost_denominator(self):
        self.seed()
        allocated = allocate_issues(self.pool["eligible"], self.record["release_slots"],
            self.state["published_history"], self.state["issue_reservations"],
            policy=self.policy, identity_state=self.state)
        for number, slot in enumerate(self.record["release_slots"][:2]):
            iid = slot["issue_id"]
            issue = allocated["issues"][number]
            # Keep genuine legacy selection/facts as original fixture bytes.
            # The summary may count saved completion metadata, but must not
            # read/rewrite these inputs or reinterpret them as current format.
            atomic_json(self.root / "data/outputs" / iid / "revision-1/package-input.json",
                {"schema_version": 1, "issue": issue, "evidence_type": "synthetic",
                 "product_facts": [self.pool["facts_by_concept"][row["concept_id"]]
                                   for row in issue["products"]]})
            self.record["drafts"][iid] = {"issue_id": iid, "client_id": "default", "revision": 1,
                "state": "draft_ready_for_review", "complete": True, "products": 10,
                "package_hash": digest([iid, 1]), "manifest_path": str(
                    self.root / "data/outputs" / iid / "revision-1/delivery-manifest.json")}
            if number:
                self.record["drafts"][iid].update(state="frozen_inputs_changed", complete=False,
                    retained_complete=True, retained_revision=1)
        value = self.summary()
        supply = value["report_supply"]; generated = value["generated_reports"]
        self.assertEqual(supply["complete_allocated_issues"], 0)
        self.assertEqual(supply["legacy_compatible_eligible_scoped_products"], 26)
        self.assertEqual(generated["current_complete_generated_issues"], 1)
        self.assertEqual(generated["retained_only_generated_issues"], 1)
        self.assertEqual(generated["complete_generated_logical_issues"], 2)
        self.assertEqual(value["shared_project_cost_average"]["known_shared_average_usd"], "0.25400000")
        self.assertIsNone(value["per_issue_cash_cost_usd"])

    def test_current_format_stock_keeps_own_hold_and_excludes_other_hold_and_used_inventory(self):
        self.seed(functional_count=4)
        free, own, other, used = self.functional_ids[:4]
        self.record["concept_ids"] = [free] + [f"c{n:03d}" for n in range(26)]
        self.record["reporting_inventory_concept_ids"] = [own, other, used]
        self.memory.put("issue_reservations", "own", {"state": "reserved", "client_id": "default",
            "issue_id": self.record["release_slots"][0]["issue_id"], "products": [{"concept_id": own}]})
        self.memory.put("issue_reservations", "other", {"state": "reserved", "client_id": "default",
            "issue_id": "other-issue", "products": [{"concept_id": other}]})
        self.memory.put("published_history", "older", {"status": "published", "client_id": "default",
            "issue_id": "older-issue", "concept_id": used})
        self.state = self.memory.snapshot()
        self.pool = build_report_pool(self.memory, as_of=NOW, recommendation_contract=RECOMMENDATION_CONTRACT)
        selected = []
        def allocate(eligible, *args, **kwargs):
            selected.append({row["concept_id"] for row in eligible})
            return allocate_issues(eligible, *args, **kwargs)
        with patch("product_radar.issue_allocation.allocate_issues", side_effect=allocate):
            value = self.summary(report_pool=self.pool)
        self.assertEqual(selected, [{free, own}])
        supply = value["report_supply"]
        self.assertEqual(supply["eligible_scoped_products"], 2)
        self.assertEqual(supply["legacy_compatible_eligible_scoped_products"], 26)
        self.assertEqual(supply["eligible_reserved_products"], 1)
        self.assertEqual(supply["available_unpublished_unreserved_products"], 1)
        self.assertEqual(value["current_research_cohort"]["stock"]["available_new_types"], 1)

    def test_optional_missing_report_pool_stays_unknown_not_zero_legacy_capacity(self):
        self.seed()
        with patch("product_radar.programme._programme_report_pool", side_effect=SafetyError("SAVED_FILE_UNAVAILABLE")):
            value = self.summary()
        self.assertEqual(value["report_supply"]["state"], "unknown")
        self.assertIsNone(value["report_supply"]["legacy_compatible_eligible_scoped_products"])
        self.assertEqual(value["optional_input_failures"], {"report_pool": "SAVED_FILE_UNAVAILABLE"})

    def test_failed_current_scope_selection_cannot_fall_back_to_mixed_legacy_stock(self):
        self.seed(functional_count=1)
        with patch("product_radar.programme._reporting_eligible", side_effect=SafetyError("SAVED_FILE_UNAVAILABLE")), \
             patch("product_radar.issue_allocation.allocate_issues",
                   side_effect=AssertionError("Do not allocate an unverified selection")):
            value = self.summary(report_pool=self.pool)
        self.assertEqual(value["report_supply"]["state"], "unknown")
        self.assertIsNone(value["report_supply"]["legacy_compatible_eligible_scoped_products"])
        self.assertNotIn("stock", value["current_research_cohort"])
        self.assertEqual(value["optional_input_failures"], {"report_selection": "SAVED_FILE_UNAVAILABLE"})

    def test_failed_compatibility_count_does_not_replace_an_explicit_supplied_plan(self):
        self.seed()
        allocations = allocate_issues(self.pool["eligible"], self.record["release_slots"],
            self.state["published_history"], self.state["issue_reservations"],
            policy=self.policy, identity_state=self.state)
        with patch("product_radar.programme._reporting_eligible", side_effect=SafetyError("SAVED_FILE_UNAVAILABLE")), \
             patch("product_radar.issue_allocation.allocate_issues",
                   side_effect=AssertionError("Do not replace the supplied plan")):
            value = self.summary(report_pool=self.pool, allocations=allocations)
        supply = value["report_supply"]
        self.assertEqual(supply["allocation_basis"], "supplied_plan")
        self.assertIsNone(supply["current_proposal_recommendation_contract"])
        self.assertIsNone(supply["legacy_compatible_eligible_scoped_products"])
        self.assertEqual(supply["complete_allocated_issues"], 2)
        self.assertEqual(value["current_research_cohort"]["stock"]["available_new_types"], 26)
        self.assertEqual(value["optional_input_failures"], {"report_selection": "SAVED_FILE_UNAVAILABLE"})

    def test_integrity_failure_is_not_hidden_as_missing_report_stock(self):
        self.seed()
        with patch("product_radar.programme._programme_report_pool",
                   side_effect=SafetyError("RESEARCH_EVENTS_INVALID")), \
             self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
            self.summary()

    def test_current_scope_integrity_failure_and_owner_interruption_are_not_optional(self):
        self.seed()
        for error in (SafetyError("RESEARCH_EVENTS_INVALID"), KeyboardInterrupt(), SystemExit(130)):
            with self.subTest(error=type(error).__name__), \
                 patch("product_radar.programme._reporting_eligible", side_effect=error), \
                 self.assertRaises(type(error)):
                self.summary(report_pool=self.pool)


if __name__ == "__main__":
    unittest.main()
