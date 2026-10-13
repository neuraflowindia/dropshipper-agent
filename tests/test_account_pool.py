"""Three-account routing/uncertainty fixtures; synthetic keys, zero network."""

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.accounts import (account_pool_status, authorise_credential_recovery, choose_account,
    bind_registered_account, context_for_account, context_for_origin, freeze_account_origin, load_account_registry, token_digest)
from product_radar.benchmark import MetaTrial
from product_radar.models import AccountBinding, SafetyError, as_production_request
from product_radar.providers.apify import submit_once
from product_radar.research_costs import collection_costs, yield_forecast
from product_radar.storage import Journal, ShortJournal, atomic_json, load_json, renewed_read_operations
from product_radar.http_io import WireResponse
from test_benchmark_foundation import CONFIG, cli, accepted, preflight, request
from test_meta_live_trial import Provider, Response


class AccountPoolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.journal = Journal(self.root / "finance")
        self.now = datetime.now(timezone.utc)
        self.registry = {"schema_version": 1, "pool_enabled": True, "accounts": []}
        self.accounts, self.tokens = {}, {}
        for number in (1, 2, 3):
            self.add_account(f"account{number}", f"fictional-provider-{number}", f"generation{number}")
        self.registry_path = self.root / "config/local/apify_accounts.json"
        atomic_json(self.registry_path, self.registry)
        self.prepared = as_production_request(request("new-work"))
        self.token_patch = patch("product_radar.accounts.load_apify_token", side_effect=self.load_token)
        self.token_patch.start(); self.addCleanup(self.token_patch.stop)

    def add_account(self, name, provider_id, generation, *, enabled=True):
        folder = f"config/local/accounts/{name}"
        entry = {"id": name, "enabled": enabled, "priority": len(self.registry["accounts"]),
                 **{field: f"{folder}/{field}.json" for field in ("binding", "profile", "handover", "credential")}}
        token = f"synthetic-{name}-private-key-1234567890"
        binding = AccountBinding("apify-" + hashlib.sha256(provider_id.encode()).hexdigest()[:24],
            "fictional-period", "benchmark", generation, token_digest(token), self.now.isoformat(), True, True)
        self.registry["accounts"].append(entry); self.accounts[name] = binding
        self.tokens[str(self.root / entry["credential"])] = token
        atomic_json(self.root / entry["binding"], {"schema_version": 1, **asdict(binding)})
        atomic_json(self.root / entry["credential"], {"synthetic": True})
        end = (self.now + timedelta(days=10)).isoformat()
        atomic_json(self.root / entry["profile"], {"schema_version": 1, "namespace": binding.namespace,
            "generation": generation, "provider_user_id": provider_id, "identity_source": "authenticated_user_read",
            "owner_account_key": name, "period_ends_at": end, "plan": "Free"})
        atomic_json(self.root / entry["handover"], {"schema_version": 2, "account_key": name, "period": binding.period,
            "period_ends_at": end, "plan": "Free", "available_usd": "5", "balance_checked_at": self.now.isoformat(),
            "independently_funded": True, "owner_account_confirmed": True, "owner_credential_confirmed": True,
            "confirmed_at": self.now.isoformat(), "provider_user_id": provider_id, "output_route_reviewed": True,
            "production_provision_usd": "4.50", "emergency_usd": "0"})
        with self.journal.locked():
            if binding.namespace not in self.journal.state()[0]:
                self.journal.setup_budget(binding.namespace, "5", production_provision="4.50")
                self.journal.adopt_production_scope(binding.namespace, {"meta": "4.50"}, "Synthetic supplied grant", "fixture-scope")
        return entry

    def load_token(self, path):
        value = self.tokens.get(str(path))
        if value is None:
            raise SafetyError("PRIVATE_KEY_INVALID")
        return value

    def select(self, prepared=None):
        with self.journal.locked():
            return choose_account(self.root, self.journal, self.registry, prepared or self.prepared, now=self.now)

    def unknown(self, account="account1", attempt="old-work"):
        prepared = as_production_request(request(attempt))
        with self.journal.locked():
            self.journal.reserve(prepared, self.accounts[account], read_reserve="0.02", account_scope=True)
            self.journal.append("submit-" + attempt, "submitting", {"attempt": attempt})
        return prepared

    def test_absent_registry_preserves_one_account_and_has_no_provider_access(self):
        value = load_account_registry(self.root, "config/local/missing.json")
        self.assertFalse(value["pool_enabled"])
        self.assertEqual([entry["id"] for entry in value["accounts"]], ["legacy"])

    def test_registry_never_accepts_plain_secrets_or_outside_private_references(self):
        for change in ({"key": "synthetic-secret"}, {"credential": "/tmp/other-provider-key"}):
            with self.subTest(change=change):
                value = deepcopy(self.registry); value["accounts"][0].update(change)
                atomic_json(self.registry_path, value)
                with self.assertRaises(SafetyError):
                    load_account_registry(self.root)

    def test_new_work_uses_priority_and_existing_scope_not_notional_funds(self):
        self.assertEqual(self.select()["id"], "account1")
        with self.journal.locked():
            self.journal.adopt_production_scope(self.accounts["account1"].namespace, {"meta": "0"}, "Fixture source exhausted", "empty-meta")
        self.assertEqual(self.select()["id"], "account2")

    def test_explicit_registered_binding_reads_outside_money_lock_and_never_resets_funds(self):
        suite = self
        class Access:
            calls = []
            def clock(inner):
                return 0
            def run_list(inner):
                suite.assertFalse(suite.journal._locked)
                inner.calls.append("runs")
                return {"data": {"items": []}}
            def identity(inner):
                suite.assertFalse(suite.journal._locked)
                inner.calls.append("identity")
                return {"data": {"id": "fictional-provider-1"}}
        transport = Access()
        prefix = self.journal.path.read_bytes()
        result = bind_registered_account(self.root, self.journal, "account1", lambda _token: transport,
                                         now=self.now, registry=self.registry)
        self.assertEqual(result.namespace, self.accounts["account1"].namespace)
        self.assertEqual(transport.calls, ["runs", "identity"])
        self.assertEqual(self.journal.path.read_bytes(), prefix)

    def _bind_period_renewal(self, *, new_key=False, expired=True, authenticated=True):
        old = self.unknown(attempt=self.prepared.attempt_id)
        original = self.accounts["account1"]
        if expired:
            self.now += timedelta(days=11)
        entry = self.registry["accounts"][0]
        if new_key:
            self.tokens[str(self.root / entry["credential"])] = "synthetic-new-authenticated-key-1234567890"
        facts = load_json(self.root / entry["handover"])
        facts.update(period="next-real-provider-period", period_ends_at=(self.now + timedelta(days=30)).isoformat(),
                     confirmed_at=self.now.isoformat(), balance_checked_at=self.now.isoformat())
        atomic_json(self.root / entry["handover"], facts)
        suite = self
        class Identity:
            def clock(inner): return 0
            def run_list(inner):
                suite.assertFalse(suite.journal._locked)
                return {"data": {"items": []}}
            def identity(inner):
                suite.assertFalse(suite.journal._locked)
                if not authenticated:
                    from product_radar.http_io import HttpFailure
                    raise HttpFailure("PROVIDER_FORBIDDEN", status=403)
                return {"data": {"id": "fictional-provider-1"}}
        current = bind_registered_account(self.root, self.journal, "account1", lambda token: Identity(), now=self.now, registry=self.registry)
        return old, original, current

    def test_actual_monthly_binding_preserves_unresolved_old_context_before_replacing_slot(self):
        prefix = self.journal.path.read_bytes()
        old, original, current = self._bind_period_renewal()
        self.assertNotEqual(current.namespace, original.namespace)
        self.assertEqual(current.generation, original.generation)
        origin_path = self.root / "config/local/apify_origins" / old.attempt_id / "origin.json"
        origin = load_json(origin_path)
        self.assertEqual(origin["namespace"], original.namespace)
        self.assertTrue(self.journal.path.read_bytes().startswith(prefix))
        with self.journal.locked():
            attempt = self.journal.state()[1][old.attempt_id]
        self.assertIsNone(attempt["charge"])
        self.assertEqual((attempt["namespace"], attempt["read_hold"]), (original.namespace, "0.02"))
        recovered = self.select(old)
        self.assertEqual(recovered["binding"].namespace, original.namespace)
        self.assertEqual(recovered["profile"]["period_ends_at"], load_json(origin_path.parent / "context.json")["profile"]["period_ends_at"])

    def test_actual_new_period_authenticated_key_preserves_old_run_generation(self):
        old, original, current = self._bind_period_renewal(new_key=True)
        self.assertNotEqual(current.generation, original.generation)
        recovered = self.select(old)
        self.assertEqual(recovered["binding"].generation, original.generation)
        self.assertTrue(Path(recovered["credential_recovery_reference"]).exists())
        self.assertNotEqual(recovered["binding"].token_digest, original.token_digest)

    def test_renaming_unexpired_period_cannot_mint_an_account_grant(self):
        with self.assertRaisesRegex(SafetyError, "ACCOUNT_RENEWAL_ORIGIN_PROOF_REQUIRED"):
            self._bind_period_renewal(expired=False)
        with self.journal.locked():
            self.assertNotIn(self.accounts["account1"].account_key + ":next-real-provider-period", self.journal.state()[0])

    def test_owner_console_label_alone_cannot_replace_unresolved_monthly_identity(self):
        with self.assertRaisesRegex(SafetyError, "ACCOUNT_RENEWAL_ORIGIN_PROOF_REQUIRED"):
            self._bind_period_renewal(authenticated=False)
        with self.journal.locked():
            self.assertEqual(len(self.journal.state()[0]), 3)

    def test_exact_attempt_guard_prevents_parallel_controller_even_before_network(self):
        guard = Journal(self.journal.root / "attempt_guards/new-work")
        controller = MetaTrial(self.root, self.journal, account_registry=self.registry)
        with guard.locked(), patch("product_radar.benchmark.load_preparation", side_effect=AssertionError("parallel collector forbidden")):
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                controller.run("new-work")
        self.assertEqual(self.journal.path.read_bytes().count(b'"kind":"submitting"'), 0)

    def test_unknown_origin_blocks_only_that_account_and_holds_do_not_move(self):
        self.unknown()
        selected = self.select()
        self.assertEqual(selected["id"], "account2")
        with self.journal.locked():
            before = self.journal.path.read_bytes()
            self.journal.reserve(self.prepared, selected["binding"], read_reserve="0.02", account_scope=True)
            self.assertTrue(self.journal.path.read_bytes().startswith(before))
            attempts = self.journal.state()[1]
        self.assertEqual(attempts["old-work"]["namespace"], self.accounts["account1"].namespace)
        self.assertEqual(attempts["old-work"]["cap"], "0.05")

    def test_legacy_global_guard_still_rejects_other_accounts(self):
        self.unknown()
        with self.journal.locked(), self.assertRaisesRegex(SafetyError, "UNRESOLVED_PROVIDER_ATTEMPT"):
            self.journal.reserve(self.prepared, self.accounts["account2"], read_reserve="0.02")

    def test_unknown_same_acquisition_cannot_choose_another_account(self):
        old = self.unknown()
        selected = self.select(old)
        self.assertEqual(selected["binding"].namespace, self.accounts["account1"].namespace)
        self.assertEqual(selected["binding"].generation, self.accounts["account1"].generation)

    def test_missing_keys_skip_only_the_unconfigured_profile_without_minting_credit(self):
        self.tokens.pop(str(self.root / self.registry["accounts"][0]["credential"]))
        before = self.journal.path.read_bytes()
        self.assertEqual(self.select()["id"], "account2")
        self.assertEqual(self.journal.path.read_bytes(), before)

    def test_duplicate_alias_is_one_financial_account_not_another_grant(self):
        self.add_account("same-account-alias", "fictional-provider-1", "alias-generation")
        with self.journal.locked():
            result = account_pool_status(self.root, self.journal, self.registry)
        self.assertEqual(result["configured_financial_accounts"], 3)
        self.assertEqual(result["accounts"][-1]["state"], "same_account_alias")
        self.assertEqual(result["accounts"][-1]["opening_usd"], "0")

    def test_registry_limits_three_real_accounts_but_allows_retained_key_versions(self):
        self.add_account("retained-version", "fictional-provider-1", "retained-generation")
        atomic_json(self.registry_path, self.registry)
        self.assertEqual(len(load_account_registry(self.root)["accounts"]), 4)
        self.add_account("fourth-actual-account", "fictional-provider-4", "fourth-generation")
        atomic_json(self.registry_path, self.registry)
        with self.assertRaisesRegex(SafetyError, "ACCOUNT_POOL_THREE_ACCOUNTS_ONLY"):
            load_account_registry(self.root)

    def test_disabled_unsupplied_account_does_not_claim_a_five_dollar_grant(self):
        value = deepcopy(self.registry)
        value["accounts"].append({"id": "not-supplied", "enabled": False, "priority": 10,
            **{field: f"config/local/missing/{field}.json" for field in ("binding", "profile", "handover", "credential")}})
        with self.journal.locked():
            result = account_pool_status(self.root, self.journal, value)
        self.assertEqual(result["accounts"][-1]["opening_usd"], "0")
        self.assertEqual(result["configured_financial_accounts"], 3)

    def test_project_totals_retain_unlisted_original_period_holds(self):
        self.unknown()
        value = deepcopy(self.registry)
        value["accounts"] = value["accounts"][1:]
        with self.journal.locked():
            result = account_pool_status(self.root, self.journal, value)
        self.assertEqual(result["unsettled_hold_usd"], "0.05")
        self.assertEqual(result["unlisted_attempt_namespaces"], 1)

    def test_frozen_origin_cannot_be_reassigned_to_better_funded_account(self):
        first = self.select(); freeze_account_origin(self.root, self.prepared, first)
        other = context_for_account(self.root, self.registry, "account2", launch=True, now=self.now)
        with self.assertRaisesRegex(SafetyError, "ACQUISITION_ACCOUNT_ORIGIN_CHANGED"):
            freeze_account_origin(self.root, self.prepared, other)

    def test_registry_new_period_does_not_move_saved_run_or_storage_allowance(self):
        context = self.select(); origin = freeze_account_origin(self.root, self.prepared, context)
        entry = self.registry["accounts"][0]
        current = replace(context["binding"], period="next-provider-period")
        atomic_json(self.root / entry["binding"], {"schema_version": 1, **asdict(current)})
        profile = load_json(self.root / entry["profile"])
        profile.update(namespace=current.namespace, period_ends_at=(self.now + timedelta(days=40)).isoformat())
        atomic_json(self.root / entry["profile"], profile)
        old = context_for_origin(self.root, self.registry, origin, now=self.now)
        self.assertEqual(old["binding"].namespace, origin["namespace"])
        self.assertEqual(old["profile"]["period_ends_at"], context["profile"]["period_ends_at"])
        with self.assertRaises(SafetyError):
            context_for_origin(self.root, self.registry, origin, launch=True, now=self.now)

    def test_controller_context_replays_original_profile_without_reselecting(self):
        with patch("product_radar.benchmark.load_preparation", return_value=self.prepared):
            controller = MetaTrial(self.root, self.journal, account_registry=self.registry, now=lambda: self.now)
            controller.context(self.prepared.attempt_id, launch=True)
            self.assertTrue(controller.short_locks)
            selected = controller.binding.namespace
            self.unknown()
            controller.context(self.prepared.attempt_id, launch=False)
            self.assertEqual(controller.binding.namespace, selected)

    def test_wrong_provider_identity_cannot_become_a_new_alias(self):
        entry = self.registry["accounts"][0]
        profile = load_json(self.root / entry["profile"]); profile["provider_user_id"] = "different-provider"
        atomic_json(self.root / entry["profile"], profile)
        with self.assertRaisesRegex(SafetyError, "ACCOUNT_IDENTITY_MISMATCH"):
            context_for_account(self.root, self.registry, "account1")

    def test_verified_replacement_recovers_same_origin_not_a_launch(self):
        original = self.unknown()
        self.add_account("replacement", "fictional-provider-1", "replacement-generation")
        self.tokens.pop(str(self.root / self.registry["accounts"][0]["credential"]))
        origin = {"account_id": "account1", "namespace": self.accounts["account1"].namespace, "generation": "generation1"}
        with self.assertRaises(SafetyError):
            context_for_origin(self.root, self.registry, origin)
        with self.journal.locked():
            prefix = self.journal.path.read_bytes()
            authorise_credential_recovery(self.root, self.journal, self.registry, "replacement", "generation1")
        context = context_for_origin(self.root, self.registry, origin)
        self.assertEqual(context["id"], "replacement")
        self.assertEqual(context["binding"].generation, "generation1")
        self.assertEqual(self.journal.path.read_bytes(), prefix)
        with self.assertRaises(SafetyError):
            context_for_origin(self.root, self.registry, origin, launch=True)
        with self.journal.locked():
            self.assertEqual(choose_account(self.root, self.journal, self.registry, original)["binding"].generation, "generation1")

    def test_new_secret_without_recovery_proof_does_not_read_old_generation(self):
        self.add_account("replacement", "fictional-provider-1", "replacement-generation")
        with self.assertRaisesRegex(SafetyError, "CREDENTIAL_RECOVERY_PROOF_REQUIRED"):
            context_for_account(self.root, self.registry, "replacement",
                expected_namespace=self.accounts["account1"].namespace, expected_generation="generation1")

    def test_short_submission_releases_money_lock_but_preserves_durable_uncertainty(self):
        selected = self.select()
        journal = ShortJournal(self.journal, account_scope=True)
        class Transport:
            def verify_token(inner, _token):
                return None
            def launch(inner, prepared):
                self.assertFalse(self.journal._locked)
                with Journal(self.journal.root).locked() as sibling:
                    self.assertEqual(sibling.state()[1][prepared.attempt_id]["submission"], "uncertain")
                return accepted(prepared)
        proof = dict(preflight(self.prepared, selected["binding"]), read_reserve_usd="0.02")
        submit_once(self.prepared, selected["binding"], selected["token"], journal, Transport(),
                    explicit_live=True, preflight=proof, short_locks=True)
        self.assertEqual(journal.state()[1][self.prepared.attempt_id]["submission"], "submitted")

    def test_lost_launch_response_retains_hold_without_posting_on_other_account(self):
        selected = self.select(); journal = ShortJournal(self.journal, account_scope=True)
        class Lost:
            calls = 0
            def verify_token(inner, _token):
                return None
            def launch(inner, _prepared):
                inner.calls += 1
                raise RuntimeError("Synthetic lost launch")
        transport = Lost(); proof = dict(preflight(self.prepared, selected["binding"]), read_reserve_usd="0.02")
        with self.assertRaises(RuntimeError):
            submit_once(self.prepared, selected["binding"], selected["token"], journal, transport,
                        explicit_live=True, preflight=proof, short_locks=True)
        self.assertEqual(self.select()["binding"].namespace, selected["binding"].namespace)
        with self.assertRaisesRegex(SafetyError, "ATTEMPT_ALREADY_EXISTS"):
            submit_once(self.prepared, selected["binding"], selected["token"], journal, transport,
                        explicit_live=True, preflight=proof, short_locks=True)
        self.assertEqual(transport.calls, 1)

    def test_complete_pool_preflight_and_capture_keep_every_network_read_unlocked(self):
        from product_radar.providers.apify import ApifyTransport
        self.unknown()
        target = self.accounts["account2"]
        suite = self
        class PoolProvider(Provider):
            def run(inner, run_id="runOne"):
                value = super().run(run_id)
                value["data"]["userId"] = "fictional-provider-2"
                return value
            def open(inner, prepared_request, timeout):
                suite.assertFalse(suite.journal._locked)
                with Journal(suite.journal.root).locked():
                    pass
                return super().open(prepared_request, timeout)
        provider = PoolProvider()
        provider.rows = [{"id": "fictional-ad", "pageName": "Fictional public page", "adCreative": "Fixture",
                          "pageId": "fixture-page", "adArchiveId": "fixture-ad"}]
        envelope = cli.prepare(CONFIG, self.prepared.attempt_id)
        envelope.update(request=self.prepared.to_dict(), config_hash=self.prepared.config_hash)
        atomic_json(self.root / "data/benchmarks/2026-10-03/new-work/prepared.json", envelope)
        controller = MetaTrial(self.root, self.journal, account_registry=self.registry,
            transport_factory=lambda token: ApifyTransport(token, provider), now=lambda: self.now)
        self.assertTrue(controller.preflight("new-work")["ready_for_live"])
        result = controller.run("new-work")
        self.assertEqual(result["actor_actual_usd"], "0.00380000")
        self.assertFalse(result.get("remote_may_run", False))
        with self.journal.locked():
            attempts = self.journal.state()[1]
        self.assertEqual(attempts["new-work"]["namespace"], target.namespace)
        self.assertEqual(attempts["old-work"]["submission"], "uncertain")
        posts = [call for call in provider.calls if call[0] == "POST"]
        controller.run("new-work", resume_only=True)
        self.assertEqual([call for call in provider.calls if call[0] == "POST"], posts)

    def test_failed_source_cost_is_included_and_positive_hold_is_not_a_bill(self):
        old = self.unknown()
        with self.journal.locked():
            self.journal.append("receipt-old-work", "receipt", {"attempt": old.attempt_id, "run_id": "failed-run", "dataset_id": None})
            self.journal.append("terminal-old-work", "terminal", {"attempt": old.attempt_id, "status": "FAILED"})
            self.journal.append("charge-old-work", "charge", {"attempt": old.attempt_id, "usd": "0.01", "evidence": "fixture-charge"})
            value = collection_costs(self.journal, [old.attempt_id])
        self.assertEqual(value["known_actual_usd"], "0.01")
        self.assertEqual(value["unsettled_hold_usd"], "0.02")
        self.assertEqual(value["rows"][0]["remote_status"], "FAILED")

    def test_holds_and_failed_collection_costs_are_distinct_and_not_double_counted(self):
        old = self.unknown()
        with self.journal.locked():
            result = collection_costs(self.journal, [old.attempt_id, old.attempt_id])
        self.assertEqual(result["physical_acquisitions"], 1)
        self.assertEqual(result["known_actual_usd"], "0")
        self.assertEqual(result["unsettled_hold_usd"], "0.05")
        self.assertFalse(result["accounting_complete"])

    def test_yield_cost_math_increases_cost_for_five_and_three_percent(self):
        result = yield_forecast(baseline_collection_usd="0.50040006", baseline_candidates=156,
            new_products_per_report=11, report_count=13, yields=("0.05", "0.03"))
        first, stress = result["scenarios"]
        self.assertEqual(first["candidates_per_report"], 220)
        self.assertEqual(stress["candidates_per_report"], 367)
        self.assertGreater(Decimal(stress["collection_total_usd"]), Decimal(first["collection_total_usd"]))
        self.assertGreater(Decimal(first["collection_total_usd"]), Decimal("6.50"))
        self.assertIsNone(first["all_in_estimate_usd"])

    def test_actual_unit_baseline_times_report_count_is_not_one_dollar_forecast(self):
        result = yield_forecast(baseline_collection_usd="0.50", baseline_candidates=100,
            new_products_per_report=5, report_count=13, yields=("0.05",))
        self.assertEqual(result["scenarios"][0]["collection_total_usd"], "6.50000000")

    def renewed_context(self, *, new_key=False, grant=True):
        old = self.unknown(attempt=self.prepared.attempt_id)
        origin_context = self.select()
        origin = freeze_account_origin(self.root, self.prepared, origin_context)
        self.now += timedelta(days=11)
        entry = self.registry["accounts"][0]
        binding = replace(origin_context["binding"], period="next-real-provider-period", checked_at=self.now.isoformat())
        if new_key:
            token = "synthetic-verified-renewal-key-1234567890"
            self.tokens[str(self.root / entry["credential"])] = token
            binding = replace(binding, generation="renewal-key-generation", token_digest=token_digest(token))
        end = (self.now + timedelta(days=30)).isoformat()
        atomic_json(self.root / entry["binding"], {"schema_version": 1, **asdict(binding)})
        profile = load_json(self.root / entry["profile"])
        profile.update(namespace=binding.namespace, generation=binding.generation, period_ends_at=end)
        atomic_json(self.root / entry["profile"], profile)
        facts = load_json(self.root / entry["handover"])
        facts.update(period=binding.period, period_ends_at=end, confirmed_at=self.now.isoformat(), balance_checked_at=self.now.isoformat())
        atomic_json(self.root / entry["handover"], facts)
        if grant:
            with self.journal.locked():
                self.journal.setup_budget(binding.namespace, "5", production_provision="4.50")
                self.journal.adopt_production_scope(binding.namespace, {"meta": "4.50"}, "Synthetic genuinely renewed period", "renewal-scope")
        with patch("product_radar.benchmark.load_preparation", return_value=old):
            controller = MetaTrial(self.root, self.journal, account_registry=self.registry, now=lambda: self.now)
            controller.context(old.attempt_id)
        return controller, old, origin, binding

    def funded_read(self, controller):
        spec = {"method": "GET", "path": "/v2/datasets/synthetic-dataset/items", "query": {"format": "json", "offset": 0, "limit": 100},
                "byte_limit": 1024}
        reference = controller.before_request(spec, "synthetic-renewed-capture")
        read = next(iter(controller.journal.state()[1][controller.prepared.attempt_id]["renewed_reads"].values()))
        return spec, reference, read

    def test_renewed_storage_read_reserves_current_funds_without_moving_original_actor_or_hold(self):
        controller, old, origin, current = self.renewed_context()
        before = controller.journal.state()[1][old.attempt_id]
        prefix = self.journal.path.read_bytes()
        _, reference, read = self.funded_read(controller)
        after = controller.journal.state()[1][old.attempt_id]
        self.assertEqual((after["namespace"], after["generation"], after["read_hold"], after["read_charge"]),
                         (before["namespace"], before["generation"], "0.02", None))
        self.assertEqual(read["namespace"], current.namespace)
        self.assertEqual(read["generation"], current.generation)
        self.assertIn(reference, read["operations"])
        self.assertTrue(self.journal.path.read_bytes().startswith(prefix))
        self.assertEqual(controller.binding.namespace, origin["namespace"])
        self.assertFalse(self.journal._locked)

    def test_missing_current_original_account_grant_does_not_borrow_another_accounts_money(self):
        controller, _, _, _ = self.renewed_context(grant=False)
        with self.assertRaisesRegex(SafetyError, "READ_FUNDING_EXHAUSTED"):
            self.funded_read(controller)
        self.assertFalse((controller.folder / "http_activity.json").exists())
        self.assertFalse(renewed_read_operations(controller.journal.state()[1]))

    def test_expired_read_never_uses_a_different_healthy_real_account(self):
        controller, _, _, _ = self.renewed_context()
        self.registry["accounts"][0]["enabled"] = False
        with self.assertRaisesRegex(SafetyError, "ORIGINAL_ACCOUNT_CURRENT_READ_FUNDS_REQUIRED"):
            self.funded_read(controller)
        self.assertEqual(len(controller.journal.state()[1]), 1)

    def test_same_real_authenticated_renewal_key_can_read_old_job_without_changing_generation(self):
        controller, _, origin, current = self.renewed_context(new_key=True)
        self.assertEqual(controller.binding.namespace, origin["namespace"])
        self.assertEqual(controller.binding.generation, origin["generation"])
        controller.transport.verify_token(self.tokens[str(self.root / self.registry["accounts"][0]["credential"])])
        self.assertTrue(Path(controller.credential_recovery_reference).exists())
        _, _, read = self.funded_read(controller)
        self.assertEqual(read["namespace"], current.namespace)
        self.assertEqual(read["generation"], current.generation)

    def test_refreshing_current_key_identity_does_not_conflict_with_old_recovery_proof(self):
        controller, old, origin, current = self.renewed_context(new_key=True)
        first_ref = controller.credential_recovery_reference
        self.now += timedelta(minutes=1)
        entry = self.registry["accounts"][0]
        atomic_json(self.root / entry["binding"], {"schema_version": 1, **asdict(replace(current, checked_at=self.now.isoformat()))})
        recovered = context_for_origin(self.root, self.registry, origin, now=self.now)
        self.assertEqual(recovered["binding"].generation, origin["generation"])
        self.assertNotEqual(recovered["credential_recovery_reference"], first_ref)
        self.assertTrue(Path(first_ref).exists())

    def test_restarted_funding_reuses_one_envelope_and_keeps_lost_reply_maximum_bytes(self):
        controller, old, origin, _ = self.renewed_context()
        _, _, first = self.funded_read(controller)
        with patch("product_radar.benchmark.load_preparation", return_value=old):
            resumed = MetaTrial(self.root, self.journal, account_registry=self.registry, now=lambda: self.now)
            resumed.context(old.attempt_id)
        _, _, second = self.funded_read(resumed)
        self.assertEqual(first["read_id"], second["read_id"])
        self.assertEqual(len(second["operations"]), 2)
        self.assertTrue(all(row["received_bytes"] is None for row in second["operations"].values()))
        self.assertEqual(len(renewed_read_operations(resumed.journal.state()[1])), 1)
        self.assertEqual(resumed.binding.namespace, origin["namespace"])

    def test_actual_saved_read_reply_binds_current_funding_and_zero_replay_duplicates(self):
        controller, old, _, _ = self.renewed_context()
        spec, reference, read = self.funded_read(controller)
        wire = WireResponse(body=b"[]", status=200, headers={}, elapsed=0.01)
        controller.save_response(spec, wire, "synthetic-renewed-capture", reference)
        controller.save_response(spec, wire, "synthetic-renewed-capture", reference)
        actual = controller.journal.state()[1][old.attempt_id]["renewed_reads"][read["read_id"]]
        self.assertEqual(actual["operations"][reference]["received_bytes"], 2)
        metadata = load_json(controller.folder / "http" / (reference + ".json"))
        self.assertEqual(metadata["funding_id"], read["read_id"])
        self.assertEqual(actual["charge"], None)

    def test_current_period_spendable_and_pool_totals_include_old_run_new_read_hold_once(self):
        controller, old, _, current = self.renewed_context()
        self.funded_read(controller)
        value = account_pool_status(self.root, controller.journal, self.registry)
        current_row = next(row for row in value["accounts"] if row["account_id"] == "account1")
        self.assertEqual(current_row["unsettled_hold_usd"], "0.02")
        self.assertEqual(value["unsettled_hold_usd"], "0.07")
        self.assertEqual(value["renewed_read_funding_operations"], 1)
        self.assertEqual(controller.journal.spendable(current.namespace), Decimal("0.05"))
        cost = collection_costs(controller.journal, [old.attempt_id, old.attempt_id])
        self.assertEqual(cost["physical_acquisitions"], 1)
        self.assertEqual(cost["unsettled_hold_usd"], "0.07")
        self.assertEqual(cost["rows"][0]["renewed_read_operations"][0]["namespace"], current.namespace)

    def test_exact_operation_settlement_cannot_reopen_original_fees_or_double_count(self):
        controller, old, origin, _ = self.renewed_context()
        _, reference, read = self.funded_read(controller)
        proof = {"evidence_type": "operator_confirmed_read_usage", "attempt": old.attempt_id, "read_id": read["read_id"],
            "namespace": read["namespace"], "generation": read["generation"], "operation_ids": [reference], "usd": "0.001",
            "checked_at": self.now.isoformat(), "reason": "Synthetic exact new-period usage attribution."}
        folder = controller.folder / "funded_reads" / read["read_id"]
        atomic_json(folder / "read_cost.json", proof)
        atomic_json(controller.folder / "result.json", {"read_actual_usd": None, "read_reserved_usd": "0.02"})
        controller.settle_renewed_reads(old.attempt_id, read["read_id"])
        count = len(controller.journal.events())
        controller.settle_renewed_reads(old.attempt_id, read["read_id"])
        self.assertEqual(len(controller.journal.events()), count)
        after = controller.journal.state()[1][old.attempt_id]
        self.assertIsNone(after["read_charge"])
        self.assertEqual(after["read_hold"], "0.02")
        self.assertEqual(after["namespace"], origin["namespace"])
        self.assertEqual(after["renewed_reads"][read["read_id"]]["charge"], "0.001")
        costs = collection_costs(controller.journal, [old.attempt_id])
        self.assertEqual(costs["known_actual_usd"], "0.001")
        self.assertEqual(costs["unsettled_hold_usd"], "0.05")
        result = load_json(controller.folder / "result.json")
        self.assertIsNone(result["read_actual_usd"])
        self.assertEqual(result["renewed_read_actual_usd"], "0.001")
        self.assertEqual(result["renewed_read_reserved_usd"], "0")
        self.assertTrue(result["renewed_read_accounting_complete"])
        with self.assertRaisesRegex(SafetyError, "READ_FUNDING_ALREADY_SETTLED"):
            self.funded_read(controller)

    def test_renewed_read_settlement_rejects_wrong_period_or_partial_operation_evidence(self):
        controller, old, _, _ = self.renewed_context()
        _, reference, read = self.funded_read(controller)
        proof = {"evidence_type": "operator_confirmed_read_usage", "attempt": old.attempt_id, "read_id": read["read_id"],
            "namespace": read["namespace"], "generation": read["generation"], "operation_ids": [reference], "usd": "0",
            "checked_at": self.now.isoformat(), "reason": "Synthetic proof only."}
        path = controller.folder / "funded_reads" / read["read_id"] / "read_cost.json"
        for change in ({"namespace": self.accounts["account2"].namespace}, {"operation_ids": []}, {"generation": "unknown-generation"}):
            atomic_json(path, dict(proof, **change))
            with self.assertRaisesRegex(SafetyError, "READ_COST_EVIDENCE_REQUIRED"):
                controller.settle_renewed_reads(old.attempt_id, read["read_id"])
        self.assertIsNone(controller.journal.state()[1][old.attempt_id]["renewed_reads"][read["read_id"]]["charge"])

    def test_lifetime_storage_attempt_limit_does_not_reset_after_renewal(self):
        controller, _, _, _ = self.renewed_context()
        for _ in range(40):
            self.funded_read(controller)
        with self.assertRaisesRegex(SafetyError, "READ_ACTIVITY_LIMIT_REVIEW_REQUIRED"):
            self.funded_read(controller)
        self.assertEqual(len(load_json(controller.folder / "http_activity.json", 262144)), 40)

    def test_real_controller_old_job_capture_uses_funded_read_and_never_launches_again(self):
        from product_radar.providers.apify import ApifyTransport
        controller, old, origin, _ = self.renewed_context()
        with self.journal.locked():
            self.journal.append("receipt-existing", "receipt", {"attempt": old.attempt_id, "run_id": "runOne", "dataset_id": "datasetOne"})
        suite = self
        class RenewalProvider(Provider):
            def run(inner, run_id="runOne"):
                value = super().run(run_id)
                value["data"]["userId"] = "fictional-provider-1"
                return value
            def open(inner, request, timeout):
                suite.assertFalse(suite.journal._locked)
                if "/datasets/" in request.full_url:
                    with Journal(suite.journal.root).locked() as reader:
                        operations = renewed_read_operations(reader.state()[1])
                        suite.assertEqual(len(operations), 1)
                        suite.assertTrue(operations[0]["operations"])
                return super().open(request, timeout)
        provider = RenewalProvider()
        factory = lambda token: ApifyTransport(token, opener=provider)
        with patch("product_radar.benchmark.load_preparation", return_value=old):
            live = MetaTrial(self.root, self.journal, account_registry=self.registry, now=lambda: self.now, transport_factory=factory)
            first = live.run(old.attempt_id, resume_only=True)
            self.assertEqual(first["code"], "CAPTURED_REVIEW_AND_READ_COST_PENDING")
            captured = sum("/datasets/" in url for method, url, _ in provider.calls)
            second = live.run(old.attempt_id, resume_only=True)
            self.assertEqual(second["captured_count"], first["captured_count"])
            self.assertEqual(sum("/datasets/" in url for method, url, _ in provider.calls), captured)
        self.assertEqual(sum(method == "POST" for method, _, _ in provider.calls), 0)
        self.assertEqual(live.journal.state()[1][old.attempt_id]["namespace"], origin["namespace"])

    def test_renewed_read_admission_failure_is_saved_before_any_get_and_does_not_erase_original(self):
        controller, old, origin, _ = self.renewed_context()
        original = controller.journal.append
        def fail_dispatch(event_id, kind, value):
            if kind == "renewed_read_dispatched":
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
            return original(event_id, kind, value)
        with patch.object(controller.journal, "append", side_effect=fail_dispatch):
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                self.funded_read(controller)
        after = controller.journal.state()[1][old.attempt_id]
        self.assertEqual(after["namespace"], origin["namespace"])
        read = next(iter(after["renewed_reads"].values()))
        self.assertIsNone(read["charge"])
        self.assertEqual(read["operations"], {})
        self.assertEqual(len(load_json(controller.folder / "http_activity.json", 262144)), 1)

    def test_renewed_read_late_fee_overrun_is_recorded_and_blocks_only_current_funding_account(self):
        controller, old, _, current = self.renewed_context()
        _, reference, read = self.funded_read(controller)
        proof = {"evidence_type": "operator_confirmed_read_usage", "attempt": old.attempt_id, "read_id": read["read_id"],
            "namespace": read["namespace"], "generation": read["generation"], "operation_ids": [reference], "usd": "0.001",
            "checked_at": self.now.isoformat(), "reason": "Synthetic exact usage."}
        path = controller.folder / "funded_reads" / read["read_id"] / "read_cost.json"
        atomic_json(path, proof); controller.settle_renewed_reads(old.attempt_id, read["read_id"])
        atomic_json(path, dict(proof, usd="0.03")); controller.settle_renewed_reads(old.attempt_id, read["read_id"])
        self.assertEqual(controller.journal.state()[1][old.attempt_id]["renewed_reads"][read["read_id"]]["charge"], "0.03")
        # Sibling accounts have genuinely later renewal dates in this fixture;
        # do not inadvertently expire all three while advancing the first.
        for entry in self.registry["accounts"][1:]:
            end = (self.now + timedelta(days=10)).isoformat()
            for field in ("profile", "handover"):
                value = load_json(self.root / entry[field]); value["period_ends_at"] = end
                atomic_json(self.root / entry[field], value)
        selected = self.select(as_production_request(request("different-new-work")))
        self.assertEqual(selected["binding"].namespace, self.accounts["account2"].namespace)
        self.assertEqual(account_pool_status(self.root, controller.journal, self.registry)["incurred_usd"], "0.03")

    def test_low_current_balance_does_not_create_renewal_funds(self):
        controller, _, _, _ = self.renewed_context()
        entry = self.registry["accounts"][0]
        facts = load_json(self.root / entry["handover"]); facts["available_usd"] = "0.50"
        atomic_json(self.root / entry["handover"], facts)
        with self.assertRaisesRegex(SafetyError, "READ_FUNDING_EXHAUSTED"):
            self.funded_read(controller)
        self.assertFalse((controller.folder / "http_activity.json").exists())


if __name__ == "__main__":
    unittest.main()
