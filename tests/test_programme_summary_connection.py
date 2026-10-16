"""Final checkpoint and read-only owner-summary connections; offline only."""

from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
from io import StringIO
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_programme as programme_fixtures
from test_catalogue_expansion import fixture as catalogue_fixture
from test_programme_summary import account_status, financial, product as report_product
from product_radar.models import AccountBinding, SafetyError, digest
from product_radar.programme import (_refresh_owner_measurements, _reporting_eligible, _reports, _signature, load_programme,
                                     programme_owner_summary, programme_progress, set_programme_service_state)
from product_radar.report_selection import build_report_pool
from product_radar.service import load_policy
from product_radar.storage import Journal, atomic_json


class ProgrammeSummaryConnectionTests(unittest.TestCase):
    def setUp(self):
        programme_fixtures.ProgrammeTests.setUp(self)
        from test_programme_cost_snapshot import baseline
        self.cost_baseline = baseline()
        self.cost_baseline["baseline"]["observed_at"] = self.now
        self.cost_baseline["selector"]["fingerprint"] = digest(self.cost_baseline["baseline"])
        atomic_json(self.root / "config/research/collection_cost_baseline.json", self.cost_baseline)

    product = programme_fixtures.ProgrammeTests.product
    prepare = programme_fixtures.ProgrammeTests.prepare
    noops = programme_fixtures.ProgrammeTests.noops
    advance = programme_fixtures.ProgrammeTests.advance
    cli = programme_fixtures.ProgrammeCliTests.cli

    def test_one_financial_pair_and_supplied_research_state_are_reused_without_probes(self):
        record = self.prepare(); saved = self.memory.snapshot(); ledger = financial(); policy = load_policy()
        calls = []
        def money():
            self.assertTrue(self.memory.journal._locked)
            return ledger
        def accounts(root, facade, *, now):
            self.assertFalse(self.memory.journal._locked)
            self.assertEqual(root, self.root)
            self.assertIs(facade.state(), ledger)
            self.assertEqual(facade.spendable(next(iter(ledger[0]))), Decimal("1.022"))
            self.assertEqual(now, self.now)
            calls.append("accounts")
            return account_status()
        def pool(facade, **kwargs):
            self.assertFalse(self.memory.journal._locked)
            self.assertIs(facade.snapshot(), saved)
            self.assertEqual(facade.evidence_type, "synthetic")
            calls.append("pool")
            return build_report_pool(facade, **kwargs)
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("Research reread")), \
             patch.object(self.memory.journal, "state", side_effect=money) as money_read, \
             patch("product_radar.accounts.account_pool_status", side_effect=accounts), \
             patch("product_radar.catalogue.load_catalogue", return_value=catalogue_fixture()), \
             patch("product_radar.report_selection.build_report_pool", side_effect=pool), \
             patch("product_radar.service.load_policy", return_value=policy), \
             patch("product_radar.programme.load_json", return_value=self.cost_baseline) as public_read, \
             patch("product_radar.operational_effort_cache.load_cached_model_effort", return_value={"model": None}) as cache_read, \
             patch("builtins.open", side_effect=AssertionError("File probe")), \
             patch.object(Path, "exists", side_effect=AssertionError("File probe")), \
             patch("urllib.request.urlopen", side_effect=AssertionError("Provider request")), \
             patch("product_radar.research_ai.CodexRunner.__call__", side_effect=AssertionError("Model request")):
            summary = programme_owner_summary(self.memory, record, root=self.root, state=saved, as_of=self.now)
        money_read.assert_called_once_with()
        public_read.assert_called_once_with(self.root / "config/research/collection_cost_baseline.json", 16 * 1024)
        cache_read.assert_called_once_with(self.memory, saved, as_of=self.now)
        self.assertEqual(calls, ["accounts", "pool"])
        self.assertEqual(summary["money"]["state"], "known")
        self.assertEqual(summary["catalogue"]["totals"]["families"], 1)
        self.assertEqual(summary["report_supply"]["allocation_state"], "known")
        self.assertEqual(summary["report_supply"]["complete_allocated_issues"], 0)
        self.assertEqual([row["shortfall"] for row in summary["report_supply"]["issue_shortfalls"]], [10, 10, 10])
        self.assertEqual(summary["optional_input_failures"], {})
        self.assertEqual(summary["effort"]["state"], "unknown")
        self.assertEqual(summary["forecast"]["state"], "known")
        self.assertEqual(summary["report_supply"]["allocation_basis"], "current_research_proposal")

    def test_missing_optional_config_does_not_hide_saved_cost_or_research_counts(self):
        record = self.prepare(); saved = self.memory.snapshot()
        with patch.object(self.memory.journal, "state", return_value=financial()), \
             patch("product_radar.accounts.account_pool_status", side_effect=SafetyError("PRIVATE_BINDING_UNAVAILABLE")), \
             patch("product_radar.catalogue.load_catalogue", side_effect=SafetyError("SAVED_FILE_UNAVAILABLE")), \
             patch("product_radar.service.load_policy", side_effect=SafetyError("SAVED_FILE_UNAVAILABLE")):
            value = programme_owner_summary(self.memory, record, root=self.root, state=saved, as_of=self.now)
        self.assertEqual(value["money"]["state"], "known")
        self.assertEqual(value["research"]["original_pool"]["products"], 2)
        self.assertEqual(value["report_supply"]["state"], "known")
        self.assertEqual(value["report_supply"]["allocation_state"], "unknown")
        self.assertEqual(value["accounts"]["state"], "unknown")
        self.assertEqual(value["catalogue"]["state"], "unknown")
        self.assertEqual(set(value["optional_input_failures"]), {"account_status", "catalogue", "allocation_policy"})

    def test_global_financial_and_research_integrity_errors_are_not_hidden_as_optional(self):
        record = self.prepare(); saved = self.memory.snapshot()
        with patch.object(self.memory.journal, "state", side_effect=SafetyError("JOURNAL_CORRUPT_REVIEW_REQUIRED")):
            with self.assertRaisesRegex(SafetyError, "JOURNAL_CORRUPT_REVIEW_REQUIRED"):
                programme_owner_summary(self.memory, record, root=self.root, state=saved, as_of=self.now)
        with patch("product_radar.report_selection.build_report_pool", side_effect=SafetyError("RESEARCH_EVENTS_INVALID")):
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
                programme_owner_summary(self.memory, record, root=self.root, state=saved, as_of=self.now,
                    financial_state=financial(), account_status=account_status(), catalogue=catalogue_fixture())

    def test_broken_optional_allocation_preserves_available_stock(self):
        record = self.prepare(); saved = self.memory.snapshot()
        pool = {"as_of": self.now, "eligible": [report_product("first"), report_product("second")], "decisions": []}
        with patch("product_radar.issue_allocation.allocate_issues", side_effect=SafetyError("ISSUE_POOL_INVALID")):
            value = programme_owner_summary(self.memory, record, root=self.root, state=saved, as_of=self.now,
                financial_state=financial(), account_status=account_status(), catalogue=catalogue_fixture(),
                report_pool=pool, policy=load_policy())
        self.assertEqual(value["report_supply"]["available_unpublished_unreserved_products"], 0)
        self.assertEqual(value["report_supply"]["legacy_compatible_eligible_scoped_products"], 2)
        self.assertIsNone(value["report_supply"]["complete_allocated_issues"])
        self.assertEqual(value["optional_input_failures"], {"allocations": "ISSUE_POOL_INVALID"})

    def test_common_reporting_filter_keeps_own_hold_and_excludes_used_external_or_unrelated_inventory(self):
        for cid in ("published", "externally-held", "own-held", "unrelated"):
            self.product(cid)
        record = self.prepare(concept_ids=["first"],
            reporting_inventory_concept_ids=["second", "published", "externally-held", "own-held"])
        self.memory.put("published_history", "publication", {"status": "published", "concept_id": "published",
            "client_id": "default", "issue_id": "earlier-issue"})
        self.memory.put("issue_reservations", "external", {"state": "reserved", "client_id": "default",
            "issue_id": "other-issue", "products": [{"concept_id": "externally-held"}]})
        self.memory.put("issue_reservations", "own", {"state": "reserved", "client_id": "default",
            "issue_id": record["release_slots"][0]["issue_id"], "products": [{"concept_id": "own-held"}]})
        saved = self.memory.snapshot()
        pool = {"as_of": self.now, "eligible": [report_product(cid) for cid in saved["concepts"]], "decisions": []}
        expected = {"first", "second", "own-held"}; selected = []
        self.assertEqual({row["concept_id"] for row in _reporting_eligible(record, saved, pool)}, expected)
        def allocate(eligible, *_args, **_kwargs):
            selected.append({row["concept_id"] for row in eligible})
            return {"issues": []}
        with patch("product_radar.report_selection.build_report_pool", return_value=pool), \
             patch("product_radar.issue_allocation.allocate_issues", side_effect=allocate):
            _reports(self.memory, record, {"at": self.now, "root": self.root, "runner": None})
            programme_owner_summary(self.memory, record, root=self.root, state=saved, as_of=self.now,
                financial_state=financial(), account_status=account_status(), catalogue=catalogue_fixture(),
                report_pool=pool, policy=load_policy())
        # The existing writer first previews a possible functional upgrade,
        # then retains its unbound legacy recovery. The automatic owner
        # proposal targets the current format, not those older selections.
        self.assertEqual(selected, [set(), expected, set()])

    def test_final_parent_checkpoint_gets_one_summary_and_reuses_status_snapshot(self):
        original = self.prepare(); self.product("newly-discovered")
        def discover(_memory, _record, _context):
            return {"state": "checked", "new_concept_ids": ["newly-discovered"]}
        with patch("product_radar.programme.programme_owner_summary", wraps=programme_owner_summary) as summaries, \
             patch("product_radar.programme.programme_progress", wraps=programme_progress) as progress:
            result = self.advance(discovery_step=discover, max_cycles=2)
        summaries.assert_called_once()
        parent_progress = next(call for call in progress.call_args_list if call.kwargs["record"]["programme_id"] == original["programme_id"])
        self.assertIs(parent_progress.kwargs["state"], summaries.call_args.kwargs["state"])
        self.assertEqual(result["owner_summary"]["research"]["original_pool"]["products"], 2)
        self.assertEqual(result["owner_summary"]["research"]["follow_on_pool"]["products"], 1)
        saved_record = load_programme(self.memory, original["programme_id"])
        self.assertEqual(saved_record["concept_ids"], original["concept_ids"])
        self.assertEqual(saved_record["owner_summary"], result["owner_summary"])
        self.assertEqual(saved_record["owner_summary"]["money"]["state"], "known")
        self.assertNotIn("financial_state", saved_record["owner_summary"]["optional_input_failures"])
        self.assertNotIn("owner_summary", saved_record["follow_on_reviews"][next(iter(saved_record["follow_on_reviews"]))])
        self.assertFalse(result["owner_summary"]["client_publication_attempted"])

    def test_summary_does_not_manufacture_useful_progress(self):
        record = self.prepare(); saved = self.memory.snapshot(); before = _signature(saved, record)
        changed = deepcopy(record)
        changed["owner_summary"] = {"as_of": self.now, "owner_only": True, "money": {"state": "unknown"}}
        changed["owner_measurement_refresh"] = {"at": self.now, "state": "partial_cached"}
        self.assertEqual(_signature(saved, changed), before)

    def test_default_connection_separates_generated_files_proposals_and_shared_average(self):
        record = self.prepare(); saved = self.memory.snapshot()
        for slot in record["release_slots"][:2]:
            iid = slot["issue_id"]
            record["drafts"][iid] = {"issue_id": iid, "client_id": record["client_id"], "revision": 1,
                "state": "draft_ready_for_review", "complete": True, "products": 10,
                "package_hash": digest([iid, 1]), "manifest_path": str(
                    self.root / "data/outputs" / iid / "revision-1/delivery-manifest.json")}
        with patch("product_radar.operational_effort_cache.refresh_model_effort_checkpoint",
                   side_effect=AssertionError("Read-only status must not refresh")), \
             patch("product_radar.operational_effort_cache.model_effort",
                   side_effect=AssertionError("No original receipt scan")), \
             patch("urllib.request.urlopen", side_effect=AssertionError("No provider")):
            value = programme_owner_summary(self.memory, record, root=self.root, state=saved, as_of=self.now,
                financial_state=financial(), account_status=account_status(), catalogue=catalogue_fixture(),
                report_pool={"eligible": [], "decisions": []}, policy=load_policy())
        self.assertEqual(value["generated_reports"]["complete_generated_logical_issues"], 2)
        self.assertEqual(value["report_supply"]["complete_allocated_issues"], 0)
        self.assertEqual(value["report_supply"]["allocation_basis"], "current_research_proposal")
        self.assertEqual(value["shared_project_cost_average"]["known_shared_average_usd"], "0.25400000")
        self.assertEqual(value["forecast"]["state"], "known")
        self.assertEqual(value["forecast"]["baseline_fingerprint"], self.cost_baseline["selector"]["fingerprint"])
        self.assertEqual(value["money"]["selected_supporting_acquisitions"], 0)
        self.assertIsNone(value["per_issue_cash_cost_usd"])

    def test_optional_baseline_or_cache_failure_keeps_other_summary_sections(self):
        record = self.prepare(); saved = self.memory.snapshot()
        for failure in ("SAVED_FILE_SIZE_LIMIT", "SYMLINK_PATH_REFUSED"):
            with self.subTest(failure=failure), \
                 patch("product_radar.programme.load_json", side_effect=SafetyError(failure)), \
                 patch("product_radar.operational_effort_cache.load_cached_model_effort",
                       side_effect=SafetyError("SYMLINK_PATH_REFUSED")):
                value = programme_owner_summary(self.memory, record, root=self.root, state=saved, as_of=self.now,
                    financial_state=financial(), account_status=account_status(), catalogue=catalogue_fixture(),
                    report_pool={"eligible": [], "decisions": []}, policy=load_policy())
            self.assertEqual(value["optional_input_failures"], {"cost_baseline": failure,
                "model_effort_checkpoint": "SYMLINK_PATH_REFUSED"})
            self.assertEqual(value["money"]["state"], "known")
            self.assertEqual(value["research"]["original_pool"]["products"], 2)
            self.assertEqual(value["forecast"]["state"], "unknown")
            self.assertEqual(self.memory.snapshot(), saved)

    def test_model_measurement_checkpoint_keeps_totals_not_individual_attempt_rows(self):
        record = self.prepare(); saved = self.memory.snapshot()
        model = {"saved_job_attempts": 1, "usage_receipts": 1, "attempts_without_usable_token_counts": 0,
            "duration_receipts": 1, "recorded_duration_sum_seconds": 2.0,
            "recorded_tokens": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10},
            "rows": [{"job_id": "private-measurement-marker"}]}
        checkpoint = {"version": "owner-model-effort-checkpoint-v1", "owner_only": True,
            "state": "partial_cached", "checked_at": self.now, "model": model,
            "coverage": {"total_jobs": 2, "current_request_bound_jobs": 1, "unprocessed_jobs": 1},
            "unavailable": {"private-measurement-marker": "MODEL_EFFORT_JOB_UNREADABLE"},
            "diagnostics": [{"reason": "MODEL_EFFORT_CACHE_ENTRY_INVALID"}]}
        value = programme_owner_summary(self.memory, record, root=self.root, state=saved, as_of=self.now,
            financial_state=financial(), account_status=account_status(), catalogue=catalogue_fixture(),
            report_pool={"eligible": [], "decisions": []}, policy=load_policy(), effort_checkpoint=checkpoint)
        self.assertEqual(value["effort"]["state"], "partial")
        self.assertEqual(value["effort"]["model"]["recorded_tokens"]["input_tokens"], 100)
        self.assertNotIn("private-measurement-marker", json.dumps(value))
        self.assertEqual(value["model_effort_checkpoint"]["unavailable_reasons"], {"MODEL_EFFORT_JOB_UNREADABLE": 1})
        self.assertEqual(value["model_effort_checkpoint"]["diagnostic_reasons"], {"MODEL_EFFORT_CACHE_ENTRY_INVALID": 1})

    def test_due_measurement_hook_is_bounded_and_cannot_make_research_progress(self):
        record = self.prepare(); saved = self.memory.snapshot(); before = _signature(saved, record)
        context = {"at": self.now, "fixed_as_of": self.now, "clock": None}
        checkpoint = {"state": "partial_cached", "coverage": {"unprocessed_jobs": 1},
            "refresh": {"probed_jobs": 25, "reduced_jobs": 20, "reused_jobs": 5, "saved": True}}
        with patch("product_radar.operational_effort_cache.refresh_model_effort_checkpoint", return_value=checkpoint) as refresh:
            _refresh_owner_measurements(self.memory, record, context, state=saved)
            _refresh_owner_measurements(self.memory, record, context, state=saved)
        refresh.assert_called_once_with(self.memory, saved, job_limit=25, as_of=self.now, minimum_interval_seconds=300)
        self.assertEqual(record["owner_measurement_refresh"]["refresh"]["probed_jobs"], 25)
        self.assertEqual(_signature(saved, record), before)
        self.assertEqual(self.memory.snapshot(), saved)

    def test_failed_optional_measurement_hook_does_not_end_healthy_programme(self):
        self.prepare(); observed = []
        steps = self.noops()
        steps["photos"] = lambda *_args: (observed.append("photos") or {"state": "nothing_new"})
        with patch("product_radar.operational_effort_cache.refresh_model_effort_checkpoint",
                   side_effect=SafetyError("SYMLINK_PATH_REFUSED")):
            result = self.advance(steps=steps)
        self.assertEqual(observed, ["photos"])
        row = load_programme(self.memory, "whole-programme")["owner_measurement_refresh"]
        self.assertEqual(row["optional_input_failures"], {"model_effort_checkpoint": "SYMLINK_PATH_REFUSED"})
        self.assertEqual(result["progress"]["products"], 2)
        self.assertFalse(result["client_publication_attempted"])

    def test_pause_during_last_stage_skips_cycle_and_final_housekeeping(self):
        self.prepare(); steps = self.noops()
        def pause(_memory, _record, _context):
            set_programme_service_state(self.memory, enabled=False, as_of=self.now)
            return {"state": "owner_paused"}
        steps["private_staging"] = pause
        with patch("product_radar.operational_effort_cache.refresh_model_effort_checkpoint",
                   side_effect=AssertionError("Paused service must not do housekeeping")):
            result = self.advance(steps=steps)
        self.assertEqual(result["tick_state"], "owner_paused")
        self.assertFalse(result["progress"]["service_enabled"])
        self.assertNotIn("owner_measurement_refresh", load_programme(self.memory, "whole-programme"))

    def test_failed_cache_save_is_observed_not_reported_as_saved(self):
        from product_radar.programme import _owner_effort_projection
        checkpoint = {"state": "partial_cached", "checked_at": self.now, "cache_checked_at": self.now,
            "cache_age_seconds": 0, "refresh": {"saved": False}, "diagnostics": [{"reason": "MODEL_EFFORT_CACHE_SAVE_FAILED"}]}
        value = _owner_effort_projection(checkpoint)
        self.assertEqual(value["measurement_persistence"], "observed_not_saved")
        self.assertIsNone(value["cache_checked_at"])
        self.assertIsNone(value["cache_age_seconds"])
        self.assertEqual(value["diagnostic_reasons"], {"MODEL_EFFORT_CACHE_SAVE_FAILED": 1})

    def test_busy_final_save_preserves_owner_summary_in_existing_pending_control(self):
        self.prepare()
        with programme_fixtures.busy_programme_saves(
                self.memory, when=lambda rows: any("owner_summary" in row for row in rows)):
            result = self.advance()
        self.assertEqual(result["control_save"]["saved"], "local_checkpoint")
        self.assertEqual(load_programme(self.memory, "whole-programme")["owner_summary"], result["owner_summary"])

    def test_default_cli_shows_fresh_summary_from_one_state_and_cannot_execute(self):
        self.prepare(); cli = self.cli(); before = self.memory.snapshot()
        control = next((self.memory.root / "programme_runs").glob("*/pending-control.json"))
        original_bytes = control.read_bytes(); stdout = StringIO()
        with patch("sys.stdout", new=stdout), \
             patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as snapshots, \
             patch.object(self.memory.journal, "state", wraps=self.memory.journal.state) as financial_reads, \
             patch.object(cli, "advance_programme", side_effect=AssertionError("Execution")), \
             patch("product_radar.catalogue_discovery.create_catalogue_discovery_step", side_effect=AssertionError("Acquisition setup")), \
             patch("product_radar.research_ai.CodexRunner.__call__", side_effect=AssertionError("Model request")), \
             patch("urllib.request.urlopen", side_effect=AssertionError("Provider request")):
            result = cli.main(["--programme-id", "whole-programme"], memory=self.memory, handlers={}, root=self.root)
        self.assertEqual(result, 0)
        self.assertEqual(snapshots.call_count, 1)
        self.assertEqual(financial_reads.call_count, 1)
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["products"], 2)
        self.assertTrue(output["owner_summary"]["owner_only"])
        self.assertEqual(output["owner_summary"]["accounts"]["approved_accounts"], 3)
        self.assertEqual(output["owner_summary"]["research"]["original_pool"]["products"], 2)
        self.assertEqual(output["owner_summary"]["money"]["state"], "known")
        self.assertNotIn("financial_state", output["owner_summary"]["optional_input_failures"])
        self.assertIsNone(output["owner_summary"]["per_issue_cash_cost_usd"])
        self.assertEqual(control.read_bytes(), original_bytes)
        self.assertEqual(self.memory.snapshot(), before)

    def test_real_financial_journal_read_is_once_and_released_before_accounts(self):
        record = self.prepare(); saved = self.memory.snapshot()
        with self.memory.journal.locked():
            self.memory.journal.setup_budget("synthetic-summary-account:saved-period", "5", "0.5")
        original = self.memory.journal.state; calls = []
        def money():
            self.assertTrue(self.memory.journal._locked)
            calls.append("money")
            return original()
        def accounts(_root, facade, *, now):
            self.assertFalse(self.memory.journal._locked)
            self.assertEqual(Decimal(facade.state()[0]["synthetic-summary-account:saved-period"]["allowance"]), Decimal("5"))
            self.assertEqual(now, self.now)
            calls.append("accounts")
            return account_status()
        with patch.object(self.memory.journal, "state", side_effect=money), \
             patch("product_radar.accounts.account_pool_status", side_effect=accounts):
            value = programme_owner_summary(self.memory, record, root=self.root, state=saved, as_of=self.now,
                catalogue=catalogue_fixture(), report_pool={"eligible": [], "decisions": []}, allocations={"issues": []})
        self.assertEqual(calls, ["money", "accounts"])
        self.assertFalse(self.memory.journal._locked)
        self.assertEqual(value["money"]["state"], "known")
        self.assertEqual(Decimal(value["money"]["saved_future_source_allocations_usd"]), Decimal("0.5"))

    def test_explicit_financial_pair_never_reads_or_locks_journal(self):
        record = self.prepare(); saved = self.memory.snapshot()
        with patch.object(self.memory.journal, "state", side_effect=AssertionError("Extra money read")), \
             patch.object(self.memory.journal, "locked", side_effect=AssertionError("Extra lock")):
            value = programme_owner_summary(self.memory, record, root=self.root, state=saved, as_of=self.now,
                financial_state=financial(), account_status=account_status(), catalogue=catalogue_fixture(),
                report_pool={"eligible": [], "decisions": []}, allocations={"issues": []})
        self.assertEqual(value["money"]["state"], "known")

    def test_already_locked_real_journal_reuses_caller_lock_without_nesting(self):
        record = self.prepare(); saved = self.memory.snapshot()
        with self.memory.journal.locked():
            with patch.object(self.memory.journal, "locked", side_effect=AssertionError("Nested money lock")), \
                 patch.object(self.memory.journal, "state", wraps=self.memory.journal.state) as reads:
                value = programme_owner_summary(self.memory, record, root=self.root, state=saved, as_of=self.now,
                    account_status=account_status(), catalogue=catalogue_fixture(),
                    report_pool={"eligible": [], "decisions": []}, allocations={"issues": []})
            self.assertTrue(self.memory.journal._locked)
        reads.assert_called_once_with()
        self.assertFalse(self.memory.journal._locked)
        self.assertEqual(value["money"]["state"], "known")

    def test_busy_real_journal_keeps_independent_research_counts_and_explicit_unknown(self):
        record = self.prepare(); saved = self.memory.snapshot()
        other = Journal(self.memory.journal.root)
        with other.locked(), patch.object(self.memory.journal, "state", wraps=self.memory.journal.state) as reads:
            value = programme_owner_summary(self.memory, record, root=self.root, state=saved, as_of=self.now,
                catalogue=catalogue_fixture(), report_pool={"eligible": [], "decisions": []}, allocations={"issues": []})
        reads.assert_not_called()
        self.assertFalse(self.memory.journal._locked)
        self.assertEqual(value["money"]["state"], "unknown")
        self.assertEqual(value["optional_input_failures"]["financial_state"], "RUNNER_ALREADY_ACTIVE")
        self.assertEqual(value["research"]["original_pool"]["products"], 2)
        self.assertEqual(self.memory.snapshot(), saved)

    def saved_summary_account(self, *, pool=False):
        """Real disposable files/ledger, with no credential file or request."""
        binding = AccountBinding("synthetic-summary-account", "saved-period", "benchmark",
            "synthetic-generation", "0" * 64, self.now, True, True)
        atomic_json(self.root / "config/local/apify_binding.json", {"schema_version": 1, **asdict(binding)})
        with self.memory.journal.locked():
            self.memory.journal.setup_budget(binding.namespace, "5", "4.5")
        if pool:
            def entry(name, enabled, folder):
                return {"id": name, "enabled": enabled, "priority": 0,
                        "binding": folder + "/apify_binding.json", "credential": folder + "/providers.env",
                        "profile": folder + "/apify_profile.json", "handover": folder + "/apify_handover.json"}
            atomic_json(self.root / "config/local/apify_accounts.json", {"schema_version": 1, "pool_enabled": True,
                "accounts": [entry("synthetic-saved", True, "config/local"),
                             entry("synthetic-unsupplied", False, "config/local/missing")]})

    def real_account_summary(self):
        record = self.prepare(); saved = self.memory.snapshot()
        return programme_owner_summary(self.memory, record, root=self.root, state=saved, as_of=self.now,
            catalogue=catalogue_fixture(), report_pool={"eligible": [], "decisions": []}, allocations={"issues": []})

    def test_project_root_finds_real_saved_original_account_even_without_optional_pool(self):
        self.saved_summary_account()
        before = self.memory.journal.path.read_bytes()
        with patch("product_radar.accounts.load_apify_token", side_effect=AssertionError("Credential read")), \
             patch("urllib.request.urlopen", side_effect=AssertionError("Provider request")):
            value = self.real_account_summary()
        self.assertNotEqual(self.root, self.memory.journal.root)
        self.assertEqual(value["accounts"]["state"], "known")
        self.assertEqual(value["accounts"]["configured_accounts"], 1)
        self.assertEqual(value["accounts"]["saved_funded_accounts"], 1)
        self.assertFalse(value["accounts"]["pool_enabled"])
        self.assertIsNone(value["accounts"]["currently_verified_funded_accounts"])
        self.assertEqual(self.memory.journal.path.read_bytes(), before)

    def test_project_root_real_pool_counts_only_saved_grants_not_unsupplied_profiles(self):
        self.saved_summary_account(pool=True)
        value = self.real_account_summary()
        self.assertTrue(value["accounts"]["pool_enabled"])
        self.assertEqual(value["accounts"]["registry_profiles"], 2)
        self.assertEqual(value["accounts"]["enabled_profiles"], 1)
        self.assertEqual(value["accounts"]["configured_accounts"], 1)
        self.assertEqual(value["accounts"]["saved_funded_accounts"], 1)

    def test_missing_saved_account_is_known_zero_but_invalid_registry_is_unknown(self):
        absent = self.real_account_summary()
        self.assertEqual(absent["accounts"]["state"], "known")
        self.assertEqual(absent["accounts"]["configured_accounts"], 0)
        self.assertEqual(absent["accounts"]["saved_funded_accounts"], 0)
        atomic_json(self.root / "config/local/apify_accounts.json", {"schema_version": 1, "accounts": []})
        invalid = self.real_account_summary()
        self.assertEqual(invalid["accounts"]["state"], "unknown")
        self.assertIsNone(invalid["accounts"]["configured_accounts"])
        self.assertIsNone(invalid["accounts"]["saved_funded_accounts"])
        self.assertEqual(invalid["optional_input_failures"]["account_status"], "ACCOUNT_REGISTRY_INVALID")


if __name__ == "__main__":
    unittest.main()
