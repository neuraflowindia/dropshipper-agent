"""Pure owner-cost arithmetic and saved-draft binding, never provider calls."""

from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from product_radar.models import digest
from product_radar.programme_cost_snapshot import BASELINE_VERSION, build_owner_cost_snapshot
from product_radar.service import load_policy
from test_programme_summary import PRIVATE, account_status, financial, record, state

NOW = "2026-10-08T18:00:00+00:00"
ROOT = Path("/tmp/cost-snapshot-public-project")


def baseline():
    value = {"schema_version": 1, "version": BASELINE_VERSION,
        "baseline_id": "historical-156-known-collection", "observed_at": "2026-10-07T10:00:00+00:00",
        "baseline_collection_usd": "0.50040006", "baseline_candidates": 156,
        "reference": "docs/testing/benchmark.md#frozen-legacy-costyield-baseline",
        "source_mix": ["meta", "amazon", "trends"], "fit_policy_id": "fit-13-v1"}
    return {"schema_version": 1, "selector": {"baseline_id": value["baseline_id"], "fingerprint": digest(value)},
            "baseline": value}


def draft(issue_id, revision=1, *, absolute=True):
    relative = f"data/outputs/{issue_id}/revision-{revision}/delivery-manifest.json"
    return {"issue_id": issue_id, "revision": revision, "state": "draft_ready_for_review", "complete": True,
        "package_hash": digest([issue_id, revision]), "manifest_path": str(ROOT / relative) if absolute else relative,
        "products": 10, "approved": False, "client_ready": False}


def packet_receipt(issue_id, revision=1):
    files = [{"name": name, "size": 100, "sha256": digest([issue_id, revision, name])}
        for name in ("shortlist.xlsx", "final_report_data.json", "delivery-manifest.json", "report-en.pdf", "report-hi-Latn.pdf")]
    return {"issue_id": issue_id, "revision": revision, "package_hash": digest([issue_id, revision]),
        "client_id": "default", "manifest_hash": digest([issue_id, revision, "manifest"]), "files": files,
        "outcomes": [dict(row, state="verified") for row in files], "verified_file_count": len(files),
        "state": "private_bytes_verified", "index_unchanged": True, "client_publication_attempted": False}


class ProgrammeCostSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.record = record(["original"]); self.record["updated_at"] = NOW
        self.record["drafts"] = {"issue-0": draft("issue-0"), "issue-1": draft("issue-1"),
                                 "issue-2": {"issue_id": "issue-2", "state": "shortfall", "complete": False, "products": 5}}
        self.state = state(); self.financial = financial(); self.policy = load_policy()

    def build(self, **overrides):
        values = {"record": self.record, "state": self.state, "financial_state": self.financial,
            "baseline": baseline(), "policy": self.policy, "account_status": account_status(),
            "as_of": NOW, "project_root": ROOT}
        values.update(overrides)
        return build_owner_cost_snapshot(**values)

    def test_failed_project_costs_divide_by_generated_issues_not_proposals_or_languages(self):
        value = self.build()
        costs = value["shared_project_collection"]
        self.assertEqual(Decimal(costs["known_incurred_usd"]), Decimal("0.508"))
        self.assertEqual(Decimal(costs["unsettled_hold_usd"]), Decimal("0.02"))
        self.assertEqual(costs["physical_acquisitions"], 2)
        self.assertFalse(costs["accounting_complete"])
        reports = value["generated_reports"]
        self.assertEqual(reports["complete_generated_logical_issues"], 2)
        self.assertEqual(reports["current_draft_states"], {"draft_ready_for_review": 2, "shortfall": 1})
        average = value["shared_project_cost_average"]
        self.assertEqual(average["known_shared_average_usd"], "0.25400000")
        self.assertTrue(average["unfinished_batch"])
        self.assertIsNone(average["exclusive_per_issue_cash_cost_usd"])
        self.assertIsNone(average["all_in_per_issue_cost_usd"])
        self.assertIsNone(value["per_issue_cash_cost_usd"])

    def test_full44_project_attempts_include_failed_and_unknown88_cent_holds(self):
        budget, examples = financial()
        attempt = examples["shared-attempt"]
        rows = {f"meta-{n}": dict(attempt, charge="0.01", stage="meta",
                    remote_status="FAILED" if n == 0 else "SUCCEEDED") for n in range(39)}
        rows.update({f"amazon-{n}": dict(attempt, charge="0.02", stage="amazon") for n in range(4)})
        rows["trends-0"] = dict(attempt, charge="0.03040006", stage="trends", remote_status="FAILED")
        value = self.build(financial_state=(budget, rows))
        costs = value["shared_project_collection"]
        self.assertEqual(costs["physical_acquisitions"], 44)
        self.assertEqual(costs["acquisitions_by_source"], {"amazon": 4, "meta": 39, "trends": 1})
        self.assertEqual(Decimal(costs["known_incurred_usd"]), Decimal("0.50040006"))
        self.assertEqual(Decimal(costs["unsettled_hold_usd"]), Decimal("0.88"))
        self.assertEqual(value["shared_project_cost_average"]["known_shared_average_usd"], "0.25020003")

    def test_unknown_actor_charge_not_imputed_as_zero_or_bill(self):
        ledger = deepcopy(self.financial); ledger[1]["failed-attempt"]["charge"] = None
        value = self.build(financial_state=ledger)
        self.assertEqual(Decimal(value["shared_project_collection"]["known_incurred_usd"]), Decimal("0.5"))
        self.assertEqual(Decimal(value["shared_project_collection"]["unsettled_hold_usd"]), Decimal("0.22"))
        self.assertIn("PROJECT_COLLECTION_CHARGES_UNSETTLED", [row["code"] for row in value["warnings"]])

    def test_missing_financial_pair_preserves_reports_and_explicit_baseline(self):
        value = self.build(financial_state=None)
        self.assertEqual(value["shared_project_collection"]["state"], "unknown")
        self.assertEqual(value["generated_reports"]["complete_generated_logical_issues"], 2)
        self.assertEqual(value["conditional_forecast"]["state"], "known")
        self.assertEqual(value["shared_project_cost_average"]["state"], "unknown")

    def test_zero_generated_reports_stays_undefined_not_zero_per_report(self):
        frozen = deepcopy(self.record); frozen["drafts"] = {}
        average = self.build(record=frozen)["shared_project_cost_average"]
        self.assertEqual(average["completed_generated_logical_issues"], 0)
        self.assertEqual(average["reason"], "COST_SNAPSHOT_NO_COMPLETE_GENERATED_ISSUES")
        self.assertIsNone(average["known_shared_average_usd"])

    def test_partial_editions_do_not_count_even_with_a_prospective_package_hash(self):
        frozen = deepcopy(self.record)
        for row in frozen["drafts"].values():
            row["complete"] = False; row["state"] = "language_pending"
            row["stage_outcomes"] = {"editions": {"en": {"state": "completed"}}}
        self.assertEqual(self.build(record=frozen)["generated_reports"]["complete_generated_logical_issues"], 0)

    def test_retained_completed_original_counts_once_not_ready_current_revision(self):
        frozen = deepcopy(self.record); row = frozen["drafts"]["issue-0"]
        row.update(state="frozen_inputs_changed", complete=False, retained_complete=True, retained_revision=1)
        reports = self.build(record=frozen)["generated_reports"]
        self.assertEqual(reports["complete_generated_logical_issues"], 2)
        self.assertEqual(reports["current_complete_generated_issues"], 1)
        self.assertEqual(reports["retained_only_generated_issues"], 1)
        self.assertEqual(reports["issues"][0]["revision"], 1)
        self.assertEqual(reports["issues"][0]["basis"], "retained_complete")

    def test_protected_original_packet_counts_without_inventing_retained_revision(self):
        frozen = deepcopy(self.record)
        frozen["drafts"]["issue-0"].update(state="protected_issue", complete=False, retained_complete=True)
        reports = self.build(record=frozen)["generated_reports"]
        self.assertEqual(reports["state"], "known")
        self.assertEqual(reports["complete_generated_logical_issues"], 2)
        self.assertEqual(reports["current_complete_generated_issues"], 1)
        self.assertEqual(reports["retained_only_generated_issues"], 1)
        self.assertEqual(reports["issues"][0]["revision"], 1)
        self.assertEqual(reports["issues"][0]["package_hash"], digest(["issue-0", 1]))
        self.assertEqual(self.build(record=frozen)["shared_project_cost_average"]["known_shared_average_usd"], "0.25400000")

    def test_protected_fallback_refuses_new_path_foreign_hash_or_missing_original_path(self):
        for changes in ({"revision": 2}, {"retained_package_hash": digest(["issue-0", 2])},
                        {"manifest_path": str(ROOT / "data/outputs/issue-0/revision-2/delivery-manifest.json")},
                        {"manifest_path": None}, {"revision": True}, {"retained_revision": None}):
            with self.subTest(changes=changes):
                frozen = deepcopy(self.record)
                row = frozen["drafts"]["issue-0"]
                row.update(state="protected_issue", complete=False, retained_complete=True, **changes)
                # Even an old good private receipt must not hide mismatched
                # original-path metadata in this narrow protected fallback.
                frozen["private_staging_receipts"] = {"original": packet_receipt("issue-0")}
                reports = self.build(record=frozen)["generated_reports"]
                self.assertEqual(reports["state"], "partial")
                self.assertIsNone(reports["complete_generated_logical_issues"])
                self.assertEqual(reports["known_generated_logical_issues"], 1)

    def test_explicit_retained_revision_stays_authoritative_for_protected_packet(self):
        frozen = deepcopy(self.record)
        row = frozen["drafts"]["issue-0"]
        row.update(state="protected_issue", complete=False, retained_complete=True, revision=2, retained_revision=1,
                   retained_package_hash=digest(["issue-0", 1]))
        reports = self.build(record=frozen)["generated_reports"]
        self.assertEqual(reports["state"], "known")
        self.assertEqual(reports["issues"][0]["revision"], 1)
        row["retained_revision"] = 2
        self.assertEqual(self.build(record=frozen)["generated_reports"]["state"], "partial")

    def test_retained_newer_partial_uses_exact_original_verified_packet_binding(self):
        frozen = deepcopy(self.record)
        frozen["drafts"]["issue-0"] = {"issue_id": "issue-0", "revision": 2, "state": "writing_pending", "complete": False,
            "retained_complete": True, "retained_revision": 1, "retained_package_hash": digest(["issue-0", 1])}
        frozen["private_staging_receipts"] = {"original-packet": packet_receipt("issue-0")}
        reports = self.build(record=frozen)["generated_reports"]
        self.assertEqual(reports["complete_generated_logical_issues"], 2)
        self.assertIn("revision-1", reports["issues"][0]["manifest_path"])
        self.assertNotIn("revision-2", reports["issues"][0]["manifest_path"])

    def test_retained_bad_or_unverified_receipt_makes_count_partial_not_invented(self):
        for field, value in (("state", "setup_waiting"), ("revision", 2), ("revision", True), ("package_hash", digest("another")),
                             ("verified_file_count", True), ("client_publication_attempted", True), ("outcomes", [])):
            with self.subTest(field=field):
                frozen = deepcopy(self.record)
                row = frozen["drafts"]["issue-0"]
                row.update(revision=2, state="writing_pending", complete=False, retained_complete=True,
                           retained_revision=1, retained_package_hash=digest(["issue-0", 1]))
                row.pop("manifest_path")
                receipt = packet_receipt("issue-0"); receipt[field] = value
                frozen["private_staging_receipts"] = {"original-packet": receipt}
                reports = self.build(record=frozen)["generated_reports"]
                self.assertEqual(reports["state"], "partial")
                self.assertIsNone(reports["complete_generated_logical_issues"])
                self.assertEqual(reports["known_generated_logical_issues"], 1)

    def test_bad_dictionary_issue_binding_and_hash_never_count_generated(self):
        for field, value in (("issue_id", "issue-1"), ("package_hash", "bad"), ("revision", True), ("complete", 1)):
            with self.subTest(field=field):
                frozen = deepcopy(self.record); frozen["drafts"]["issue-0"][field] = value
                reports = self.build(record=frozen)["generated_reports"]
                self.assertEqual(reports["state"], "partial")
                self.assertEqual(reports["known_generated_logical_issues"], 1)
                self.assertIsNone(reports["complete_generated_logical_issues"])
                self.assertIsNone(self.build(record=frozen)["shared_project_cost_average"]["known_shared_average_usd"])

    def test_complete_packet_must_bind_exact_slot_and_manifest_path(self):
        for path in (str(ROOT / "data/outputs/issue-1/revision-1/delivery-manifest.json"),
                     str(ROOT / "data/outputs/issue-0/revision-2/delivery-manifest.json"),
                     "/tmp/another-project/data/outputs/issue-0/revision-1/delivery-manifest.json",
                     "data/outputs/issue-0/../issue-0/revision-1/delivery-manifest.json",
                     "data//outputs/issue-0/revision-1/delivery-manifest.json", "https://private.example/file"):
            with self.subTest(path=path):
                frozen = deepcopy(self.record); frozen["drafts"]["issue-0"]["manifest_path"] = path
                self.assertEqual(self.build(record=frozen)["generated_reports"]["state"], "partial")
        frozen = deepcopy(self.record); frozen["drafts"]["not-a-release-slot"] = draft("not-a-release-slot")
        self.assertEqual(self.build(record=frozen)["generated_reports"]["state"], "partial")

    def test_absolute_paths_need_supplied_root_and_only_relative_paths_are_exported(self):
        self.assertEqual(self.build(project_root=None)["generated_reports"]["state"], "partial")
        frozen = deepcopy(self.record)
        frozen["drafts"]["issue-0"] = draft("issue-0", absolute=False)
        frozen["drafts"]["issue-1"] = draft("issue-1", absolute=False)
        reports = self.build(record=frozen, project_root=None)["generated_reports"]
        self.assertEqual(reports["state"], "known")
        self.assertTrue(all(row["manifest_path"].startswith("data/outputs/") for row in reports["issues"]))

    def test_missing_draft_store_is_unknown_not_false_zero(self):
        frozen = deepcopy(self.record); frozen.pop("drafts")
        reports = self.build(record=frozen)["generated_reports"]
        self.assertEqual(reports["state"], "unknown")
        self.assertIsNone(self.build(record=frozen)["shared_project_cost_average"]["known_shared_average_usd"])

    def test_revisions_and_repeated_editions_never_inflate_logical_count(self):
        frozen = deepcopy(self.record); frozen["drafts"]["issue-0"] = draft("issue-0", 7)
        frozen["drafts"]["issue-0"]["languages"] = ["en", "hi-Latn"]
        reports = self.build(record=frozen)["generated_reports"]
        self.assertEqual(reports["complete_generated_logical_issues"], 2)
        self.assertEqual(reports["issues"][0]["revision"], 7)

    def test_actual_october_calendar_is14_with_low_yield_and_all_in_unknown(self):
        forecast = self.build()["conditional_forecast"]
        self.assertEqual(forecast["calendar_issue_count"], 14)
        self.assertEqual(forecast["month"], "2026-10")
        self.assertEqual(forecast["report_count"], 14)
        self.assertEqual(forecast["baseline_fingerprint"], baseline()["selector"]["fingerprint"])
        planning, stress = forecast["scenarios"]
        self.assertEqual(planning["candidates_total"], 2800)
        self.assertEqual(stress["candidates_total"], 4676)
        self.assertEqual(planning["collection_total_usd"], "8.98153954")
        self.assertEqual(stress["collection_total_usd"], "14.99917103")
        self.assertIsNone(planning["all_in_estimate_usd"])
        self.assertTrue(forecast["allowance_fit_not_inferred"])
        self.assertIn("not measured", forecast["caveats"][2])

    def test_forecast_never_uses_incomplete_batch_average_as_unit_cost(self):
        frozen = deepcopy(self.record); frozen["drafts"]["issue-2"] = draft("issue-2")
        more = self.build(record=frozen)
        self.assertNotEqual(more["shared_project_cost_average"]["known_shared_average_usd"], self.build()["shared_project_cost_average"]["known_shared_average_usd"])
        self.assertEqual(more["conditional_forecast"], self.build()["conditional_forecast"])

    def test_india_month_boundary_not_utc_month_or_four_week_assumption(self):
        value = self.build(as_of="2026-10-31T19:00:00+00:00")["conditional_forecast"]
        self.assertEqual(value["month"], "2026-11")
        self.assertEqual(value["calendar_issue_count"], 12)
        value = self.build(as_of="2026-12-31T19:00:00+00:00")["conditional_forecast"]
        self.assertEqual(value["month"], "2027-01")
        self.assertEqual(value["calendar_end_exclusive"], "2027-02-01")

    def test_missing_invalid_or_foreign_selector_baseline_is_named_unknown(self):
        variants = [None]
        for field, value in (("baseline_collection_usd", "0"), ("baseline_collection_usd", "NaN"),
                             ("baseline_candidates", True), ("baseline_candidates", 0), ("baseline_candidates", 100001),
                             ("observed_at", "2027-01-01T00:00:00Z"), ("fit_policy_id", "fit-10-v1"),
                             ("reference", "../private-account.json"), ("source_mix", ["meta", "meta"]),
                             ("source_mix", ["meta", ["amazon"]])):
            wrapper = baseline(); wrapper["baseline"][field] = value
            wrapper["selector"]["fingerprint"] = digest(wrapper["baseline"])
            variants.append(wrapper)
        wrapper = baseline(); wrapper["selector"]["fingerprint"] = digest("wrong"); variants.append(wrapper)
        wrapper = baseline(); wrapper["selector"]["baseline_id"] = "another"; variants.append(wrapper)
        for wrapper in variants:
            with self.subTest(wrapper=wrapper):
                result = self.build(baseline=wrapper)
                self.assertEqual(result["conditional_forecast"]["state"], "unknown")
                self.assertIn("BASELINE", result["conditional_forecast"]["reason"])
                self.assertEqual(result["shared_project_collection"]["state"], "known")

    def test_invalid_policy_time_and_calendar_range_never_blocks_other_sections(self):
        for changes in ({"policy": None}, {"policy": {"timezone": "invalid"}}, {"as_of": "2026-10-08"},
                        {"as_of": "9999-12-31T00:00:00Z"}):
            with self.subTest(changes=changes):
                value = self.build(**changes)
                self.assertEqual(value["conditional_forecast"]["state"], "unknown")
                self.assertEqual(value["shared_project_collection"]["state"], "known")
                self.assertEqual(value["generated_reports"]["complete_generated_logical_issues"], 2)

    def test_actual_one_account_is_separate_from_approved_three_not_inferred_monthly_fit(self):
        value = self.build()
        self.assertEqual(value["funding"]["approved_accounts"], 3)
        self.assertEqual(value["funding"]["configured_accounts"], 1)
        self.assertEqual(value["funding"]["saved_funded_accounts"], 1)
        self.assertIsNone(value["funding"]["currently_verified_funded_accounts"])
        self.assertIn("APPROVED_ACCOUNT_POOL_NOT_FULLY_FUNDED", [row["code"] for row in value["warnings"]])
        self.assertNotIn("fits_allowance", value["conditional_forecast"])

    def test_supporting_lineage_uses_original_paid_attempt_not_dataset_id(self):
        saved = deepcopy(self.state)
        saved["associations"]["associated"] = {"concept_id": "original", "semantic_id": "semantic-product", "status": "resolved"}
        saved["observations"]["observation"] = {"capture_id": "capture", "semantic_id": "semantic-product",
            "acquisition_id": "provider-dataset-not-financial-id", "observed_at": NOW}
        saved["captures"]["capture"] = {"observed_at": NOW, "reference": {"attempt_id": "shared-attempt"},
                                             "acquisition_id": "provider-dataset-not-financial-id"}
        result = self.build(state=saved)
        lineage = result["supporting_paid_capture_lineage"]
        self.assertEqual(lineage["paid_attempt_ids"], ["shared-attempt"])
        self.assertEqual(Decimal(lineage["known_incurred_usd"]), Decimal("0.5"))
        self.assertEqual(Decimal(result["shared_project_collection"]["known_incurred_usd"]), Decimal("0.508"))

    def test_lineage_deduplicates_replays_and_excludes_foreign_future_and_direct_capture(self):
        saved = deepcopy(self.state)
        for n, (cid, at, reference) in enumerate((
                ("original", NOW, {"attempt_id": "shared-attempt"}), ("original", NOW, {"attempt_id": "shared-attempt"}),
                ("foreign", NOW, {"attempt_id": "failed-attempt"}),
                ("original", "2026-10-09T00:00:00Z", {"attempt_id": "failed-attempt"}),
                ("original", NOW, {"receipt": "direct-marketplace-receipt"}))):
            saved["associations"][f"a-{n}"] = {"concept_id": cid, "semantic_id": f"s-{n}", "status": "resolved"}
            saved["observations"][f"o-{n}"] = {"capture_id": f"c-{n}", "semantic_id": f"s-{n}", "observed_at": at}
            saved["captures"][f"c-{n}"] = {"observed_at": at, "reference": reference}
        lineage = self.build(state=saved)["supporting_paid_capture_lineage"]
        self.assertEqual(lineage["physical_acquisitions"], 1)
        self.assertEqual(lineage["paid_attempt_ids"], ["shared-attempt"])

    def test_bad_optional_lineage_does_not_erase_full_project_failed_costs(self):
        saved = deepcopy(self.state)
        saved["associations"]["a"] = {"concept_id": "original", "semantic_id": "s", "status": "resolved"}
        saved["observations"]["o"] = {"capture_id": "c", "semantic_id": "s", "observed_at": NOW}
        saved["captures"]["c"] = {"observed_at": NOW, "reference": {"attempt_id": "missing-original"}}
        value = self.build(state=saved)
        self.assertEqual(value["supporting_paid_capture_lineage"]["state"], "partial")
        self.assertIsNone(value["supporting_paid_capture_lineage"]["known_incurred_usd"])
        self.assertEqual(Decimal(value["shared_project_collection"]["known_incurred_usd"]), Decimal("0.508"))

    def test_snapshot_is_pure_deterministic_does_not_mutate_or_disclose_private_inputs(self):
        original = deepcopy((self.record, self.state, self.financial))
        with patch("builtins.open", side_effect=AssertionError("No files")), \
             patch.object(Path, "glob", side_effect=AssertionError("No scans")), \
             patch("product_radar.storage.Journal.state", side_effect=AssertionError("No ledger read")), \
             patch("product_radar.research_memory.ResearchMemory.snapshot", side_effect=AssertionError("No state read")):
            value = self.build(); self.assertEqual(value, self.build())
        self.assertEqual((self.record, self.state, self.financial), original)
        self.assertNotIn(PRIVATE, json.dumps(value))
        self.assertNotIn(str(ROOT), json.dumps(value))
        self.assertFalse(value["client_publication_attempted"])

    def test_actual_public_selected_baseline_is_accepted_without_extra_scope(self):
        # The caller loads this public <=16KB configuration, not the pure helper.
        path = Path(__file__).resolve().parents[1] / "config/research/collection_cost_baseline.json"
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        value = self.build(baseline=wrapper)["conditional_forecast"]
        self.assertEqual(value["state"], "known")
        self.assertEqual(value["baseline_collection_usd"], "0.50040006")
        self.assertEqual(value["baseline_candidates"], 156)
        self.assertEqual(value["baseline_fingerprint"], digest(wrapper["baseline"]))

    def test_renewed_read_charges_and_unsettled_holds_stay_in_project_total(self):
        ledger = deepcopy(self.financial)
        ledger[1]["shared-attempt"]["renewed_reads"] = {
            "read-1": {"read_id": "read-1", "namespace": PRIVATE, "generation": "generation", "charge": "0.003", "cap": "0.02"},
            "read-2": {"read_id": "read-2", "namespace": PRIVATE, "generation": "generation", "charge": None, "cap": "0.01"}}
        value = self.build(financial_state=ledger)["shared_project_collection"]
        self.assertEqual(Decimal(value["known_incurred_usd"]), Decimal("0.511"))
        self.assertEqual(Decimal(value["unsettled_hold_usd"]), Decimal("0.03"))
        self.assertFalse(value["accounting_complete"])

    def test_lineage_includes_frozen_follow_on_and_inventory_associations(self):
        frozen = deepcopy(self.record); saved = deepcopy(self.state)
        frozen["reporting_inventory_concept_ids"] = ["carry-over"]
        frozen["follow_on_reviews"] = {"child": {"concept_ids": ["fresh-one"]}}
        for cid, attempt_id in (("carry-over", "shared-attempt"), ("fresh-one", "failed-attempt")):
            saved["associations"][cid] = {"concept_id": cid, "semantic_id": cid, "status": "resolved"}
            saved["observations"][cid] = {"semantic_id": cid, "capture_id": cid, "observed_at": NOW}
            saved["captures"][cid] = {"observed_at": NOW, "reference": {"attempt_id": attempt_id}}
        lineage = self.build(record=frozen, state=saved)["supporting_paid_capture_lineage"]
        self.assertEqual(lineage["paid_attempt_ids"], ["failed-attempt", "shared-attempt"])
        self.assertEqual(Decimal(lineage["known_incurred_usd"]), Decimal("0.508"))


if __name__ == "__main__":
    unittest.main()
