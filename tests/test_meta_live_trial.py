"""Fictional provider/account evidence only; no real account or network access."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.accounts import bind_account, load_binding, load_handover, save_binding
from product_radar.benchmark import ACTOR_ID, MetaTrial, actual_charge, checked_pricing, review_allows_next_sample
from product_radar.http_io import HttpFailure, perform_raw, validate_spec, worker_call
from product_radar.models import SafetyError, decode_json, digest
from product_radar.providers.apify import ApifyTransport, map_meta_rows
from product_radar.storage import Journal, atomic_json, load_json

spec = importlib.util.spec_from_file_location("live_cli_test", ROOT / "scripts/benchmark_providers.py")
cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
CONFIG = load_json(ROOT / "config/benchmarks/meta_first.json")
TOKEN = "fictional-test-key-1234567890"


class Response:
    def __init__(self, data, headers=None, raw=None):
        self.raw = raw if raw is not None else json.dumps(data, default=float).encode()
        self.headers, self.status = headers or {}, 200
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, size): return self.raw[:size]


class Provider:
    def __init__(self):
        self.calls, self.rows, self.lost, self.identity_denied = [], [], False, False
        self.status, self.charge, self.cap = "SUCCEEDED", Decimal("0.0038"), "0.03"
        self.restart, self.total_override, self.headerless = 0, None, False
        self.recovery_items, self.inputs, self.fail_reads = [], {}, 0
        self.raw_override = None
        self.dataset_absent = False
        self.properties = {"urls": {"editor": "requestListSources", "type": "array"},
                           "limitPerSource": {"type": "integer"}, "scrapeAdDetails": {"type": "boolean"},
                           "runTag": {"type": "string"}, "scrapePageAds.countryCode": {"default": "ALL"}}
        self.actor = {"data": {"id": ACTOR_ID, "actorPermissionLevel": "LIMITED_PERMISSIONS", "pricingInfos": [
            {"startedAt": "2026-08-01T00:00:00+00:00", "pricingModel": "PAY_PER_EVENT", "minimalMaxTotalChargeUsd": 0.00075,
             "pricingPerEvent": {"actorChargeEvents": {"apify-default-dataset-item": {"eventPriceUsd": 0.00075},
                                                       "apify-actor-start": {"eventPriceUsd": 0.00005}}}}]}}
        self.build = {"data": {"id": CONFIG["expected_build_id"], "buildNumber": CONFIG["build"], "status": "SUCCEEDED",
                               "actorDefinition": {"input": {"required": ["urls"], "properties": self.properties}}}}
    def run(self, run_id="runOne"):
        data = {"id": run_id, "actId": ACTOR_ID, "userId": "userOne", "buildId": CONFIG["expected_build_id"],
                "defaultDatasetId": "datasetOne", "defaultKeyValueStoreId": "store" + run_id, "status": self.status,
                "options": {"timeoutSecs": 300, "maxTotalChargeUsd": self.cap}, "stats": {"restartCount": self.restart}}
        if self.charge is not None: data["usageTotalUsd"] = self.charge
        if self.dataset_absent: data["defaultDatasetId"] = None
        return {"data": data}
    def factory(self, token):
        assert token == TOKEN
        return ApifyTransport(token, self)
    def open(self, request, timeout):
        parsed = urlsplit(request.full_url); path, query = parsed.path, parse_qs(parsed.query)
        self.calls.append((request.method, path, query))
        if request.method == "POST":
            self.inputs["storerunOne"] = json.loads(request.data)
            if self.lost: raise URLError("fictional lost reply")
            return Response(self.run())
        if path == "/v2/users/me":
            if self.identity_denied: raise HTTPError(request.full_url, 403, "", {}, None)
            return Response({"data": {"id": "userOne"}})
        if path == "/v2/actor-runs":
            offset = int(query.get("offset", [0])[0])
            return Response({"data": {"items": self.recovery_items[offset:offset+50], "offset": offset,
                                      "total": len(self.recovery_items)}})
        if path.startswith("/v2/actor-runs/"):
            if self.fail_reads:
                self.fail_reads -= 1
                raise HTTPError(request.full_url, 429, "", {"Retry-After": "1"}, None)
            return Response(self.run(path.rsplit("/", 1)[1]))
        if path.startswith("/v2/key-value-stores/"):
            return Response(self.inputs[path.split("/")[3]])
        if path.startswith("/v2/actor-builds/"): return Response(self.build)
        if path.startswith("/v2/acts/"): return Response(self.actor)
        if path.startswith("/v2/datasets/"):
            offset, limit = int(query["offset"][0]), int(query["limit"][0])
            rows = self.rows[offset:offset+limit]
            headers = {"x-apify-pagination-"+k: str(v) for k, v in {
                "offset": offset, "limit": limit, "count": len(rows), "total": len(self.rows) if self.total_override is None else self.total_override}.items()}
            return Response(rows, {} if self.headerless else headers, self.raw_override)
        raise AssertionError("Unexpected fictional endpoint")


class TrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="radar-live-fake-", dir=os.environ.get("RADAR_TEST_STORAGE_ROOT"))
        self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        self.now = datetime.now(timezone.utc); self.ticks = 0
        self.provider = Provider()
        self.journal = Journal(self.root / "data/benchmarks/state/apify")
        self.attempt = "meta-one"
        self.envelope = cli.prepare(CONFIG, self.attempt)
        atomic_json(self.root / "data/benchmarks/2026-10-03" / self.attempt / "prepared.json", self.envelope)
        path = self.root / "config/local/providers.env"; path.parent.mkdir(parents=True)
        path.write_text("APIFY_TOKEN=" + TOKEN + "\n")
        self.now = datetime.now(timezone.utc)
        self.facts = {"schema_version": 2, "account_key": "testing-owner-label", "period": "provider-period-one",
            "period_ends_at": (self.now+timedelta(days=30)).isoformat(), "plan": "Free", "available_usd": "5",
            "balance_checked_at": self.now.isoformat(), "independently_funded": True, "owner_account_confirmed": True,
            "owner_credential_confirmed": True, "confirmed_at": self.now.isoformat(), "provider_user_id": "userOne",
            "output_route_reviewed": True, "production_provision_usd": "0", "emergency_usd": "0"}
        self.save_facts()
    def save_facts(self): atomic_json(self.root / "config/local/apify_handover.json", self.facts)
    def sleep(self, duration): self.ticks += duration
    def controller(self): return MetaTrial(self.root, self.journal, transport_factory=self.provider.factory,
                                            clock=lambda: self.ticks, sleep=self.sleep, now=lambda: self.now)
    @property
    def folder(self): return self.journal.root / self.attempt
    def bind(self): return bind_account(self.root, self.journal, self.provider.factory, self.now)
    def start(self):
        self.bind(); runner = self.controller(); runner.preflight(self.attempt)
        return runner.run(self.attempt)
    def review_file(self, verdict="FunctionalPass", amount="0.00001", **changes):
        state = self.journal.state()[1][self.attempt]
        snapshot = load_json(self.folder / "capture_manifest.json")
        proof = {"evidence_type": "operator_confirmed_account_usage", "attempt": self.attempt,
                 "namespace": state["namespace"], "generation": state["generation"], "usd": amount,
                 "reason": "Fictional isolated account usage proof; no actual billing.", "checked_at": self.now.isoformat()}
        atomic_json(self.folder / "read-proof.json", proof)
        record = {"schema_version": 1, "attempt": self.attempt, "snapshot_hash": snapshot["captured_hash"],
            "inspected_at": self.now.isoformat(), "controls_checked": True, "schema_checked": True,
            "scope_checked": True, "commercial_relevance_checked": True, "defects": [], "verdict": verdict,
            "read_cost_usd": amount, "read_cost_evidence": "read-proof"}
        record.update(changes); atomic_json(self.folder / "operator_review.json", record)
    def good_rows(self, count=5):
        self.provider.rows = [{"ad_archive_id": str(n+1), "page_id": "2", "is_active": True,
                               "start_date": 1750000000, "snapshot": {"title": "Kitchen organiser", "body": {"text": "Storage tray"}}}
                              for n in range(count)]

    def finished_controller(self):
        self.good_rows(); self.bind()
        controller = self.controller()
        controller.preflight(self.attempt); controller.run(self.attempt)
        self.review_file(); controller.review(self.attempt)
        atomic_json(self.root / "data/benchmarks/2026-10-03/meta-two/prepared.json", cli.prepare(CONFIG, "meta-two"))
        return controller

    def status_calls(self):
        return sum(method == "GET" and path.startswith("/v2/actor-runs/")
                   for method, path, *_ in self.provider.calls)

    def test_finished_poll_and_two_preflights_reuse_recent_known_bill(self):
        with self.journal.locked():
            controller = self.finished_controller()
            before = self.status_calls()
            controller.preflight("meta-two"); controller.preflight("meta-two")
            self.assertEqual(self.status_calls(), before)
            self.assertEqual(controller.refresh_costs(reuse_recent=True)["reused_attempts"], 1)
            self.assertEqual(sum(method == "POST" for method, *_ in self.provider.calls), 1)

    def test_billing_window_expires_and_records_delayed_higher_bill(self):
        with self.journal.locked():
            controller = self.finished_controller()
            before = self.journal.spendable(controller.binding.namespace)
            calls = self.status_calls()
            self.provider.charge = Decimal("0.02"); self.ticks += 300
            controller.preflight("meta-two")
            self.assertEqual(self.status_calls(), calls + 1)
            self.assertEqual(self.journal.state()[1][self.attempt]["charge"], "0.02000000")
            self.assertEqual(self.journal.spendable(controller.binding.namespace), before - Decimal("0.0162"))
            self.assertEqual(len([event for event in self.journal.events() if event["kind"] == "charge_update"]), 1)

    def test_forced_billing_refresh_ignores_recent_observation(self):
        with self.journal.locked():
            controller = self.finished_controller(); calls = self.status_calls()
            for _ in range(2):
                result = controller.refresh_costs()
                self.assertEqual((result["checked_attempts"], result["reused_attempts"]), (1, 0))
            self.assertEqual(self.status_calls(), calls + 2)

    def test_unknown_bill_is_rechecked_without_releasing_holds(self):
        with self.journal.locked():
            self.good_rows(); self.provider.charge = None; self.start()
            controller = self.controller(); controller.context(self.attempt)
            calls = self.status_calls()
            for _ in range(2):
                result = controller.refresh_costs(reuse_recent=True)
                self.assertEqual((result["checked_attempts"], result["reused_attempts"]), (1, 0))
            self.assertEqual(self.status_calls(), calls + 2)
            state = self.journal.state()[1][self.attempt]
            self.assertIsNone(state["charge"]); self.assertIsNone(state["read_charge"])
            self.assertEqual(state["read_hold"], "0.02")
            self.assertEqual(controller._billing_observations, {})
            atomic_json(self.root / "data/benchmarks/2026-10-03/meta-two/prepared.json", cli.prepare(CONFIG, "meta-two"))
            with self.assertRaises(SafetyError): controller.preflight("meta-two")
            self.assertEqual(sum(method == "POST" for method, *_ in self.provider.calls), 1)

    def test_missing_bill_after_known_bill_does_not_refresh_observation_age(self):
        with self.journal.locked():
            controller = self.finished_controller(); self.provider.charge = None
            controller.refresh_costs()
            self.assertEqual(controller._billing_observations, {})
            calls = self.status_calls()
            self.assertEqual(controller.refresh_costs(reuse_recent=True)["checked_attempts"], 1)
            self.assertEqual(self.status_calls(), calls + 1)
            self.assertEqual(self.journal.state()[1][self.attempt]["charge"], "0.00380000")

    def test_controller_restart_refreshes_known_bill_again(self):
        with self.journal.locked():
            self.finished_controller(); restarted = self.controller(); calls = self.status_calls()
            restarted.preflight("meta-two")
            self.assertEqual(self.status_calls(), calls + 1)
            restarted.preflight("meta-two")
            self.assertEqual(self.status_calls(), calls + 1)

    def test_billing_observation_requires_exact_identity_and_journal_state(self):
        with self.journal.locked():
            controller = self.finished_controller()
            state = self.journal.state()[1][self.attempt]
            self.assertTrue(controller._recent_billing(self.attempt, state))
            changes = {"namespace": "other-namespace", "generation": "other-generation", "run_id": "other-run",
                       "remote_status": "FAILED", "charge": "0.005", "controls_ok": False,
                       "config_hash": "0" * 64, "submission": "uncertain"}
            for field, value in changes.items():
                with self.subTest(field=field):
                    self.assertFalse(controller._recent_billing(self.attempt, {**state, field: value}))
            self.assertFalse(controller._recent_billing("other-attempt", state))
            original = controller.binding
            controller.binding = replace(original, period="different-current-period")
            self.assertFalse(controller._recent_billing(self.attempt, state))
            controller.binding = original
            self.ticks -= 1
            self.assertFalse(controller._recent_billing(self.attempt, state))

    def test_invalid_forced_response_evicts_reuse_and_recovery_rechecks(self):
        with self.journal.locked():
            controller = self.finished_controller()
            invalid = self.provider.run(); invalid["data"]["actId"] = "wrong-actor"
            with patch.object(self.provider, "run", return_value=invalid):
                with self.assertRaisesRegex(SafetyError, "REMOTE_IDENTITY_OR_STATUS_INVALID"):
                    controller.refresh_costs()
            self.assertEqual(controller._billing_observations, {})
            calls = self.status_calls()
            self.assertEqual(controller.refresh_costs(reuse_recent=True)["checked_attempts"], 1)
            self.assertEqual(self.status_calls(), calls + 1)

    def test_late_overrun_is_recorded_and_blocks_next_preflight(self):
        with self.journal.locked():
            controller = self.finished_controller(); self.ticks += 300
            self.provider.charge = Decimal("0.04")
            with self.assertRaisesRegex(SafetyError, "PRIOR_SAMPLE_REVIEW_REQUIRED"):
                controller.preflight("meta-two")
            self.assertEqual(self.journal.state()[1][self.attempt]["charge"], "0.04000000")
            self.assertEqual(controller._billing_observations, {})
            self.assertEqual(sum(method == "POST" for method, *_ in self.provider.calls), 1)

    def test_newly_finished_job_uses_ordinary_poll_instead_of_duplicate_refresh(self):
        with self.journal.locked():
            controller = self.finished_controller()
            controller.preflight("meta-two"); controller.run("meta-two")
            calls = self.status_calls()
            result = controller.refresh_costs(reuse_recent=True)
            self.assertEqual((result["checked_attempts"], result["reused_attempts"]), (0, 2))
            self.assertEqual(self.status_calls(), calls)

    def test_interrupted_receipt_save_cannot_create_reusable_billing_observation(self):
        with self.journal.locked():
            controller = self.finished_controller()
            def interrupt(path, value):
                if path.name == "terminal.json": raise OSError("fictional interrupted save")
                return atomic_json(path, value)
            with patch("product_radar.benchmark.atomic_json", side_effect=interrupt):
                with self.assertRaises(OSError): controller.refresh_costs()
            self.assertEqual(controller._billing_observations, {})
            calls = self.status_calls()
            self.assertEqual(controller.refresh_costs(reuse_recent=True)["checked_attempts"], 1)
            self.assertEqual(self.status_calls(), calls + 1)

    def test_complete_fake_workflow_review_and_no_relaunch_or_read(self):
        self.good_rows()
        with self.journal.locked():
            self.assertEqual(self.start()["verdict"], "Partial")
            self.assertEqual(self.journal.state()[1][self.attempt]["cap"], "0.05")
            self.assertFalse(self.journal.can_switch_key())
            self.review_file(); self.assertEqual(self.controller().review(self.attempt)["verdict"], "FunctionalPass")
            self.assertTrue(self.journal.can_switch_key())
            calls = len(self.provider.calls)
            self.assertEqual(self.controller().run(self.attempt)["verdict"], "FunctionalPass")
            self.assertEqual(len(self.provider.calls), calls)
        self.assertEqual(sum(c[0] == "POST" for c in self.provider.calls), 1)
        self.assertNotIn(TOKEN, self.journal.path.read_text())
    def test_missing_handover_stops_before_key_or_network(self):
        (self.root / "config/local/apify_handover.json").unlink()
        with self.journal.locked(), patch("product_radar.accounts.load_apify_token", side_effect=AssertionError("key read")):
            with self.assertRaisesRegex(SafetyError, "ACCOUNT_HANDOVER_REQUIRED"): self.bind()
        self.assertEqual(self.provider.calls, [])
    def test_cli_network_actions_require_live_and_handover(self):
        with patch.object(cli, "ROOT", self.root), patch("builtins.print") as output, patch("product_radar.accounts.load_apify_token", side_effect=AssertionError("key read")):
            for action in ("bind", "preflight", "run", "resume"):
                self.assertEqual(cli.main([action, "--attempt-id", self.attempt] if action != "bind" else [action]), 2)
            (self.root / "config/local/apify_handover.json").unlink()
            self.assertEqual(cli.main(["bind", "--live"]), 2)
        self.assertIn("ACCOUNT_HANDOVER_REQUIRED", " ".join(str(call) for call in output.call_args_list))
    def test_identity_403_needs_stable_console_id(self):
        self.provider.identity_denied = True
        self.facts["provider_user_id"] = None; self.save_facts()
        with self.journal.locked():
            with self.assertRaisesRegex(SafetyError, "CONSOLE_ACCOUNT_ID_REQUIRED"): self.bind()
            self.facts["provider_user_id"] = "userOne"; self.save_facts()
            self.assertEqual(self.bind().role, "benchmark")
    def test_existing_credential_binds_without_replacement(self):
        original = (self.root / "config/local/providers.env").read_bytes()
        with self.journal.locked():
            self.assertEqual(self.bind().role, "benchmark")
        self.assertEqual((self.root / "config/local/providers.env").read_bytes(), original)
        self.assertNotIn("owner_key_change_confirmed", self.facts)
        self.assertFalse(any(method == "POST" for method, *_ in self.provider.calls))
    def test_credential_reuse_still_requires_genuine_confirmation(self):
        self.facts["owner_credential_confirmed"] = False; self.save_facts()
        with self.journal.locked(), patch("product_radar.accounts.load_apify_token", side_effect=AssertionError("key read")):
            with self.assertRaisesRegex(SafetyError, "ACCOUNT_HANDOVER_REQUIRED"): self.bind()
        self.assertEqual(self.provider.calls, [])
    def test_legacy_confirmed_handover_is_read_without_rewriting(self):
        self.facts["schema_version"] = 1
        self.facts["owner_key_change_confirmed"] = self.facts.pop("owner_credential_confirmed")
        self.save_facts()
        data = load_handover(self.root / "config/local/apify_handover.json", self.now)
        self.assertEqual(data["schema_version"], 2)
        self.assertIs(data["owner_credential_confirmed"], True)
        self.assertNotIn("owner_key_change_confirmed", data)
        self.assertEqual(load_json(self.root / "config/local/apify_handover.json"), self.facts)
    def test_legacy_unconfirmed_handover_does_not_gain_authority(self):
        self.facts["schema_version"] = 1
        self.facts["owner_key_change_confirmed"] = False
        del self.facts["owner_credential_confirmed"]; self.save_facts()
        with self.assertRaisesRegex(SafetyError, "ACCOUNT_HANDOVER_REQUIRED"):
            load_handover(self.root / "config/local/apify_handover.json", self.now)
    def test_current_monthly_usage_route_is_read_only_and_bounded(self):
        calls = []
        def opened(request, timeout):
            calls.append((request.method, urlsplit(request.full_url).path))
            return Response({"data": {"usageCycle": {"startAt": self.now.isoformat()},
                            "totalUsageCreditsUsdAfterVolumeDiscount": "0"}})
        transport = ApifyTransport(TOKEN, opener=type("UsageOpener", (), {"open": staticmethod(opened)})())
        self.assertEqual(transport.monthly_usage()["data"]["totalUsageCreditsUsdAfterVolumeDiscount"], "0")
        self.assertEqual(calls, [("GET", "/v2/users/me/usage/monthly")])
        spec = {"method": "GET", "path": "/v2/users/me/usage/monthly", "query": None,
                "body": None, "byte_limit": 524288, "token": TOKEN}
        for changed in ({"method": "POST"}, {"query": {"date": "2026-09-01"}}, {"path": "/v2/users/me/usage/daily"}):
            with self.assertRaisesRegex(SafetyError, "INVALID_PROVIDER_REQUEST"): validate_spec({**spec, **changed})
        # The monthly limits read (W11) is a reviewed plain GET too: no write, no query.
        validate_spec({**spec, "path": "/v2/users/me/limits"})
        for changed in ({"method": "POST"}, {"query": {"date": "2026-09-01"}}):
            with self.assertRaisesRegex(SafetyError, "INVALID_PROVIDER_REQUEST"):
                validate_spec({**spec, "path": "/v2/users/me/limits", **changed})
    def test_identity_mismatch_cannot_fallback(self):
        self.facts["provider_user_id"] = "differentUser"; self.save_facts()
        with self.journal.locked(), self.assertRaisesRegex(SafetyError, "ACCOUNT_IDENTITY_MISMATCH"): self.bind()
    def test_rebind_preserves_baseline_not_latest_balance(self):
        with self.journal.locked():
            original = self.bind()
            self.facts["available_usd"] = "4"; self.facts["account_key"] = "renamed-owner-label"; self.save_facts()
            changed = self.bind()
            self.assertEqual(original.namespace, changed.namespace)
            self.assertEqual(self.journal.state()[0][original.namespace]["allowance"], "5")
    def test_owner_acknowledgement_does_not_expire_with_balance(self):
        self.facts["confirmed_at"] = (self.now-timedelta(days=2)).isoformat(); self.save_facts()
        self.assertTrue(load_handover(self.root / "config/local/apify_handover.json", self.now))
    def test_dated_balance_does_not_expire_but_future_balance_and_expired_period_block(self):
        self.facts["balance_checked_at"] = (self.now-timedelta(days=2)).isoformat(); self.save_facts()
        dated = load_handover(self.root / "config/local/apify_handover.json", self.now)
        self.assertEqual(dated["balance_checked_at"], self.facts["balance_checked_at"])
        for field, value, code in (("balance_checked_at", (self.now+timedelta(days=2)).isoformat(), "HANDOVER_TIMESTAMP_FUTURE"),
                                   ("period_ends_at", self.now.isoformat(), "PROVIDER_PERIOD_EXPIRED")):
            old = self.facts[field]; self.facts[field] = value; self.save_facts()
            with self.assertRaisesRegex(SafetyError, code): load_handover(self.root / "config/local/apify_handover.json", self.now)
            self.facts[field] = old
    def test_funding_role_does_not_require_obsolete_fixed_provisions(self):
        self.facts["independently_funded"] = False; self.save_facts()
        self.assertTrue(load_handover(self.root / "config/local/apify_handover.json", self.now))
    def test_shared_account_allocation_and_ballpark_buffer(self):
        self.facts.update(independently_funded=False, production_provision_usd="3.85", emergency_usd="0.75",
                          available_usd="4.35"); self.save_facts()
        with self.journal.locked():
            self.bind()
            self.assertTrue(self.controller().preflight(self.attempt)["ready_for_live"])
            self.facts["available_usd"] = "4.34999"; self.save_facts()
            self.bind()
            with self.assertRaisesRegex(SafetyError, "CURRENT_BALANCE_INSUFFICIENT"):
                self.controller().preflight(self.attempt)
        self.assertFalse(any(method == "POST" for method, *_ in self.provider.calls))
    def test_metadata_changes_stop_before_any_post(self):
        self.provider.properties["limitPerSource"]["type"] = "string"
        with self.journal.locked():
            self.bind()
            with self.assertRaisesRegex(SafetyError, "PUBLIC_METADATA_CHANGED_REVIEW_REQUIRED"): self.controller().preflight(self.attempt)
        self.assertFalse(any(c[0] == "POST" for c in self.provider.calls))
    def test_pricing_future_entry_is_not_current(self):
        self.provider.actor["data"]["pricingInfos"].append({"startedAt": (self.now+timedelta(days=1)).isoformat(), "pricingModel": "RENTAL"})
        with self.journal.locked():
            self.bind(); self.assertTrue(self.controller().preflight(self.attempt)["ready_for_live"])
    def test_low_fresh_balance_blocks_launch(self):
        self.facts["available_usd"] = "0.04"; self.save_facts()
        with self.journal.locked(), self.assertRaisesRegex(SafetyError, "BUDGET_EXHAUSTED"): self.start()
        self.assertFalse(any(c[0] == "POST" for c in self.provider.calls))
    def test_failed_run_keeps_rows_and_known_bill(self):
        self.good_rows(); self.provider.status = "FAILED"
        with self.journal.locked():
            result = self.start(); self.assertEqual(result["captured_count"], 5)
            self.assertEqual(self.journal.state()[1][self.attempt]["charge"], "0.00380000")
            self.review_file()
            with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_ACCEPTANCE_INCOMPLETE"): self.controller().review(self.attempt)
    def test_wrong_controls_do_not_hide_bill_or_rows(self):
        self.good_rows(); self.provider.cap = None
        with self.journal.locked():
            self.assertEqual(self.start()["captured_count"], 5)
            state = self.journal.state()[1][self.attempt]
            self.assertFalse(state["controls_ok"]); self.assertIsNotNone(state["charge"])
    def test_actor_overrun_is_preserved_but_cannot_pass(self):
        self.good_rows(); self.provider.charge = Decimal("0.031")
        with self.journal.locked():
            self.start(); self.review_file()
            with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_ACCEPTANCE_INCOMPLETE"): self.controller().review(self.attempt)
            self.assertEqual(self.journal.state()[1][self.attempt]["charge"], "0.03100000")
    def test_read_overrun_cannot_pass(self):
        self.good_rows()
        with self.journal.locked():
            self.start(); self.review_file(amount="0.021")
            with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_ACCEPTANCE_INCOMPLETE"): self.controller().review(self.attempt)
    def test_unknown_bill_retains_actor_reservation(self):
        self.provider.charge = None
        with self.journal.locked():
            self.assertIsNone(self.start()["actor_actual_usd"])
            self.assertFalse(self.journal.can_switch_key())
    def test_empty_is_not_commercial_functional_pass(self):
        with self.journal.locked():
            self.assertEqual(self.start()["captured_count"], 0); self.review_file()
            with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_ACCEPTANCE_INCOMPLETE"): self.controller().review(self.attempt)
    def test_rejected_rows_are_not_a_valid_empty_observation(self):
        self.provider.rows = [{"error": "not product data"}]
        with self.journal.locked():
            result = self.start(); self.assertEqual(result["mapped_count"], 0)
            self.assertFalse(load_json(self.folder / "mapped_rows.json")["empty_scope_observation"])
    def test_pagination_headers_required_and_raw_preserved(self):
        self.good_rows(); self.provider.headerless = True
        with self.journal.locked(): self.assertEqual(self.start()["code"], "DATASET_PAGINATION_UNVERIFIED")
        self.assertTrue(list((self.folder / "http").glob("*.body.json")))
        self.assertFalse((self.folder / "capture_manifest.json").exists())
    def test_inconsistent_page_total_blocks(self):
        self.good_rows(); self.provider.total_override = 20
        with self.journal.locked(): self.assertEqual(self.start()["code"], "DATASET_PAGINATION_INCONSISTENT")
    def test_paged_capture_truncation_is_explicit(self):
        self.good_rows(1005)
        with self.journal.locked():
            result = self.start(); self.assertTrue(result["truncated"]); self.assertEqual(result["captured_count"], 1000)
            self.review_file(verdict="QualityPass", amount=None, read_cost_evidence=None)
            with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_ACCEPTANCE_INCOMPLETE"):
                self.controller().review(self.attempt)
        self.assertEqual(sum(c[1].startswith("/v2/datasets/") for c in self.provider.calls), 10)
    def test_interrupted_progress_reuses_orphan_page(self):
        self.good_rows()
        original = atomic_json
        def interrupted(path, value):
            if Path(path).name == "capture_progress.json": raise SafetyError("FICTIONAL_DISK_INTERRUPTION")
            return original(path, value)
        with self.journal.locked():
            with patch("product_radar.benchmark.atomic_json", side_effect=interrupted):
                self.assertEqual(self.start()["code"], "FICTIONAL_DISK_INTERRUPTION")
            self.assertEqual(self.controller().run(self.attempt, resume_only=True)["captured_count"], 5)
        self.assertEqual(sum(c[1].startswith("/v2/datasets/") for c in self.provider.calls), 1)
    def test_completed_capture_resume_does_not_reread_pages(self):
        self.good_rows(105)
        with self.journal.locked():
            self.start(); self.controller().run(self.attempt, resume_only=True)
        self.assertEqual(sum(c[1].startswith("/v2/datasets/") for c in self.provider.calls), 2)
    def test_saved_page_corruption_blocks_resume_without_recollection(self):
        self.good_rows()
        with self.journal.locked():
            self.start(); progress = load_json(self.folder / "capture_progress.json")
            raw = self.folder / "http" / (progress["pages"][0]["response"] + ".body.json")
            raw.write_bytes(b"[]")
            self.assertEqual(self.controller().run(self.attempt, resume_only=True)["code"], "SAVED_RESPONSE_HASH_MISMATCH")
        self.assertEqual(sum(c[1].startswith("/v2/datasets/") for c in self.provider.calls), 1)
    def test_lost_post_reply_unique_recovery_never_reposts(self):
        self.good_rows(); self.provider.lost = True
        with self.journal.locked():
            self.assertEqual(self.start()["code"], "PROVIDER_NETWORK_ERROR")
            self.provider.recovery_items = [{"id": "runOne", "actId": ACTOR_ID}]
            self.assertEqual(self.controller().run(self.attempt, resume_only=True)["captured_count"], 5)
        self.assertEqual(sum(c[0] == "POST" for c in self.provider.calls), 1)
    def test_lost_post_reply_no_match_keeps_unknown_money(self):
        self.provider.lost = True
        with self.journal.locked():
            self.start(); result = self.controller().run(self.attempt, resume_only=True)
            self.assertEqual(result["code"], "RECOVERY_UNIQUE_MATCH_NOT_ESTABLISHED")
            self.assertIsNone(self.journal.state()[1][self.attempt]["charge"])
    def test_multiple_recovery_matches_block(self):
        self.provider.lost = True
        with self.journal.locked():
            self.start()
            self.provider.recovery_items = [{"id": n, "actId": ACTOR_ID} for n in ("runOne", "runTwo")]
            self.provider.inputs["storerunTwo"] = self.provider.inputs["storerunOne"]
            self.assertEqual(self.controller().run(self.attempt, resume_only=True)["code"], "RECOVERY_UNIQUE_MATCH_NOT_ESTABLISHED")
    def test_saved_launch_bytes_recover_without_input_read(self):
        self.good_rows()
        original = self.journal.append
        def interrupted(event, kind, data):
            if kind == "receipt": raise SafetyError("FICTIONAL_RECEIPT_DISK_ERROR")
            return original(event, kind, data)
        with self.journal.locked():
            with patch.object(self.journal, "append", side_effect=interrupted): self.start()
            self.assertEqual(self.controller().run(self.attempt, resume_only=True)["captured_count"], 5)
        self.assertFalse(any(c[1].startswith("/v2/key-value-stores/") for c in self.provider.calls))
    def test_recovery_ignores_expired_launch_preflight_and_access_age(self):
        self.provider.lost = True
        with self.journal.locked():
            self.start(); self.provider.recovery_items = [{"id": "runOne", "actId": ACTOR_ID}]
            self.now += timedelta(hours=2)
            self.assertEqual(self.controller().run(self.attempt)["verdict"], "Partial")
        self.assertEqual(sum(c[0] == "POST" for c in self.provider.calls), 1)
    def test_same_key_refresh_with_unresolved_job_preserves_namespace(self):
        self.provider.lost = True
        with self.journal.locked():
            self.start(); old = load_binding(self.root / "config/local/apify_binding.json")
            fresh = replace(old, checked_at=(self.now+timedelta(hours=1)).isoformat())
            save_binding(self.root / "config/local/apify_binding.json", fresh, self.journal)
            self.assertEqual(load_binding(self.root / "config/local/apify_binding.json").namespace, old.namespace)
    def test_actual_rebind_with_read_hold_keeps_original_attempt(self):
        self.good_rows()
        with self.journal.locked():
            self.start(); self.review_file(verdict="QualityPass", amount=None, read_cost_evidence=None)
            self.controller().review(self.attempt)
            before = self.journal.state()[1]
            old = load_binding(self.root / "config/local/apify_binding.json")
            self.assertEqual(self.bind().namespace, old.namespace)
            self.assertEqual(self.journal.state()[1], before)
    def test_console_balance_reused_during_day_not_repeated_approval(self):
        self.facts["balance_checked_at"] = (self.now-timedelta(hours=12)).isoformat(); self.save_facts()
        self.assertTrue(load_handover(self.root / "config/local/apify_handover.json", self.now))
    def test_read_retry_is_bounded_but_post_is_not_retried(self):
        self.provider.fail_reads = 2
        with self.journal.locked(): self.start()
        self.assertEqual(sum(c[1] == "/v2/actor-runs/runOne" for c in self.provider.calls), 3)
        self.assertEqual(sum(c[0] == "POST" for c in self.provider.calls), 1)
    def test_running_job_times_out_without_new_launch(self):
        self.provider.status = "RUNNING"
        with self.journal.locked():
            result = self.start(); self.assertEqual(result["code"], "REMOTE_WAIT_LIMIT_REACHED")
            self.assertTrue(result["remote_may_run"]); self.assertLessEqual(self.ticks, 360)
        self.assertEqual(sum(c[0] == "POST" for c in self.provider.calls), 1)
    def test_changed_key_blocks_recovery(self):
        self.provider.lost = True
        with self.journal.locked():
            self.start(); (self.root / "config/local/providers.env").write_text("APIFY_TOKEN=another-fictional-key-1234\n")
            with self.assertRaisesRegex(SafetyError, "KEY_CHANGED_RECHECK_REQUIRED"): self.controller().run(self.attempt, resume_only=True)
    def test_review_hash_conflict_cannot_accept(self):
        self.good_rows()
        with self.journal.locked():
            self.start(); self.review_file(snapshot_hash="0"*64)
            with self.assertRaisesRegex(SafetyError, "REVIEW_RECORD_INVALID"): self.controller().review(self.attempt)
    def test_cost_reconciliation_is_independent_of_failed_quality(self):
        with self.journal.locked():
            self.start(); self.review_file(verdict="Partial", defects=["Empty fictional sample"])
            self.assertEqual(self.controller().review(self.attempt)["verdict"], "Partial")
            self.assertEqual(self.journal.state()[1][self.attempt]["read_charge"], "0.00001")
    def test_quality_pass_retains_unknown_read_hold_and_allows_only_preflight(self):
        self.good_rows()
        with self.journal.locked():
            self.start(); self.review_file(verdict="QualityPass", amount=None, read_cost_evidence=None)
            self.assertEqual(self.controller().review(self.attempt)["verdict"], "QualityPass")
            state = self.journal.state()[1][self.attempt]
            self.assertIsNone(state["read_charge"]); self.assertEqual(state["read_hold"], "0.02")
            self.assertFalse(self.journal.can_switch_key())
            envelope = cli.prepare(CONFIG, "meta-two")
            atomic_json(self.root / "data/benchmarks/2026-10-03/meta-two/prepared.json", envelope)
            self.assertTrue(self.controller().preflight("meta-two")["ready_for_live"])
            self.assertEqual(sum(method == "POST" for method, *_ in self.provider.calls), 1)
            self.assertEqual(len(self.journal.state()[1]), 1)
    def test_quality_pass_does_not_bypass_retained_read_budget(self):
        self.good_rows()
        with self.journal.locked():
            self.start(); self.review_file(verdict="QualityPass", amount=None, read_cost_evidence=None)
            self.controller().review(self.attempt)
            envelope = cli.prepare(CONFIG, "meta-two")
            atomic_json(self.root / "data/benchmarks/2026-10-03/meta-two/prepared.json", envelope)
            self.facts["available_usd"] = "0.06999"; self.save_facts()
            with self.assertRaisesRegex(SafetyError, "CURRENT_BALANCE_INSUFFICIENT"):
                self.controller().preflight("meta-two")
    def test_quality_pass_cannot_promote_without_read_accounting(self):
        self.good_rows()
        with self.journal.locked():
            self.start(); self.review_file(verdict="QualityPass", amount=None, read_cost_evidence=None)
            self.controller().review(self.attempt)
            self.review_file(verdict="FunctionalPass", amount=None, read_cost_evidence=None)
            with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_ACCEPTANCE_INCOMPLETE"):
                self.controller().review(self.attempt)
    def test_quality_pass_can_promote_after_verified_read_settlement(self):
        self.good_rows()
        with self.journal.locked():
            self.start(); self.review_file(verdict="QualityPass", amount=None, read_cost_evidence=None)
            self.controller().review(self.attempt)
            self.review_file(verdict="FunctionalPass")
            self.assertEqual(self.controller().review(self.attempt)["verdict"], "FunctionalPass")
            self.assertEqual(self.journal.state()[1][self.attempt]["read_charge"], "0.00001")
    def test_quality_pass_rejects_failed_job(self):
        self.good_rows(); self.provider.status = "FAILED"
        with self.journal.locked():
            self.start(); self.review_file(verdict="QualityPass", amount=None, read_cost_evidence=None)
            with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_ACCEPTANCE_INCOMPLETE"):
                self.controller().review(self.attempt)
    def test_quality_pass_rejects_unknown_actor_bill(self):
        self.good_rows(); self.provider.charge = None
        with self.journal.locked():
            self.start(); self.review_file(verdict="QualityPass", amount=None, read_cost_evidence=None)
            with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_ACCEPTANCE_INCOMPLETE"):
                self.controller().review(self.attempt)
    def test_quality_pass_rejects_bad_controls(self):
        self.good_rows(); self.provider.cap = None
        with self.journal.locked():
            self.start(); self.review_file(verdict="QualityPass", amount=None, read_cost_evidence=None)
            with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_ACCEPTANCE_INCOMPLETE"):
                self.controller().review(self.attempt)
    def test_quality_pass_rejects_known_actor_overrun(self):
        self.good_rows(); self.provider.charge = Decimal("0.031")
        with self.journal.locked():
            self.start(); self.review_file(verdict="QualityPass", amount=None, read_cost_evidence=None)
            with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_ACCEPTANCE_INCOMPLETE"):
                self.controller().review(self.attempt)
    def test_quality_pass_rejects_empty_sample(self):
        with self.journal.locked():
            self.start(); self.review_file(verdict="QualityPass", amount=None, read_cost_evidence=None)
            with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_ACCEPTANCE_INCOMPLETE"):
                self.controller().review(self.attempt)
    def test_quality_pass_rejects_missing_manual_inspection(self):
        self.good_rows()
        with self.journal.locked():
            self.start()
            for key in ("controls_checked", "schema_checked", "scope_checked", "commercial_relevance_checked"):
                with self.subTest(check=key):
                    self.review_file(verdict="QualityPass", amount=None, read_cost_evidence=None, **{key:False})
                    with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_ACCEPTANCE_INCOMPLETE"):
                        self.controller().review(self.attempt)
            self.assertIsNone(self.journal.state()[1][self.attempt]["review"])
    def test_quality_pass_rejects_unresolved_review_defect(self):
        self.good_rows()
        with self.journal.locked():
            self.start(); self.review_file(verdict="QualityPass", amount=None, read_cost_evidence=None,
                                           defects=["Fictional unresolved schema defect"])
            with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_ACCEPTANCE_INCOMPLETE"):
                self.controller().review(self.attempt)
    def test_later_control_defect_invalidates_previously_accepted_quality(self):
        self.good_rows()
        with self.journal.locked():
            self.start(); self.review_file(verdict="QualityPass", amount=None, read_cost_evidence=None)
            self.controller().review(self.attempt)
            self.journal.append("later-bad-controls", "controls_defect", {"attempt":self.attempt, "evidence":"fictional-controls"})
            state = self.journal.state()[1][self.attempt]
            self.assertFalse(review_allows_next_sample(state))
            envelope = cli.prepare(CONFIG, "meta-two")
            atomic_json(self.root / "data/benchmarks/2026-10-03/meta-two/prepared.json", envelope)
            with self.assertRaisesRegex(SafetyError, "PRIOR_SAMPLE_REVIEW_REQUIRED"):
                self.controller().preflight("meta-two")
    def test_quality_pass_financial_event_rejects_unknown_actor_charge(self):
        self.good_rows(); self.provider.charge = None
        with self.journal.locked():
            self.start(); state = self.journal.state()[1][self.attempt]
            with self.assertRaisesRegex(SafetyError, "JOURNAL_STATE_INVALID"):
                self.journal.append("fake-quality", "review", {"attempt":self.attempt, "verdict":"QualityPass",
                                    "snapshot_hash":state["capture"], "evidence":"fictional-review"})
    def test_absent_dataset_can_settle_without_fabricated_capture(self):
        self.provider.dataset_absent = True; self.provider.status = "FAILED"
        with self.journal.locked():
            self.assertEqual(self.start()["code"], "TERMINAL_DATASET_ABSENT")
            state = self.journal.state()[1][self.attempt]
            self.assertEqual(state["capture_status"], "provider_confirmed_no_output")
            proof = {"evidence_type": "operator_confirmed_account_usage", "attempt": self.attempt,
                     "namespace": state["namespace"], "generation": state["generation"], "usd": "0",
                     "reason": "Fictional verified no storage reads", "checked_at": self.now.isoformat()}
            atomic_json(self.folder / "read_cost.json", proof)
            self.controller().settle(self.attempt)
            self.assertTrue(self.journal.can_switch_key())
        self.assertFalse((self.folder / "captured_rows.json").exists())
    def test_confirmed_non_submission_needs_exact_console_proof(self):
        self.provider.lost = True
        with self.journal.locked():
            self.start(); state = self.journal.state()[1][self.attempt]
            with self.assertRaisesRegex(SafetyError, "SAVED_FILE_UNAVAILABLE"): self.controller().resolve_non_submission(self.attempt)
            # Journal uses real UTC audit timestamps; establish later review.
            self.now = datetime.now(timezone.utc) + timedelta(seconds=1)
            proof = {"evidence_type": "operator_confirmed_non_submission", "attempt": self.attempt,
                     "namespace": state["namespace"], "generation": state["generation"], "config_hash": state["config_hash"],
                     "checked_at": self.now.isoformat(), "reason": "Fictional Console verification, not search absence", "console_verified": True, "read_cost_usd": "0"}
            atomic_json(self.folder / "non_submission.json", proof)
            self.assertEqual(self.controller().resolve_non_submission(self.attempt)["code"], "VERIFIED_NOT_SUBMITTED")
            self.assertEqual(self.journal.state()[1][self.attempt]["charge"], "0")
        self.assertEqual(sum(c[0] == "POST" for c in self.provider.calls), 1)
    def test_unsafe_restart_count_cannot_pass(self):
        self.provider.restart = 1
        with self.journal.locked():
            self.assertEqual(self.start()["code"], "REMOTE_IDENTITY_OR_STATUS_INVALID")
            self.assertIsNone(self.journal.state()[1][self.attempt]["charge"])
    def test_exact_original_wire_bytes_are_retained(self):
        self.good_rows(1)
        self.provider.raw_override = b'[\n  {"ad_archive_id":"1", "page_id":"2", "is_active":true,"start_date":1750000000}\n]'
        with self.journal.locked():
            self.start(); page = load_json(self.folder / "capture_progress.json")["pages"][0]
        self.assertEqual((self.folder / "http" / (page["response"] + ".body.json")).read_bytes(), self.provider.raw_override)
    def test_malformed_wire_data_is_saved_but_not_captured(self):
        self.provider.raw_override = b'[{"ad_archive_id":"1","ad_archive_id":"2"}]'
        with self.journal.locked(): self.assertEqual(self.start()["code"], "PROVIDER_RESPONSE_INVALID")
        self.assertFalse((self.folder / "captured_rows.json").exists())
    def test_review_rejects_changed_mapping(self):
        self.good_rows()
        with self.journal.locked():
            self.start(); self.review_file(); atomic_json(self.folder / "mapped_rows.json", {"records": [{"invented": True}]})
            with self.assertRaisesRegex(SafetyError, "REVIEW_MAPPING_MISMATCH"): self.controller().review(self.attempt)
    def test_settled_capture_still_checks_original_bytes_offline(self):
        self.good_rows()
        with self.journal.locked():
            self.start(); self.review_file(); self.controller().review(self.attempt)
            page = load_json(self.folder / "capture_progress.json")["pages"][0]
            (self.folder / "http" / (page["response"] + ".body.json")).write_bytes(b"[]")
            previous = len(self.provider.calls)
            self.assertEqual(self.controller().run(self.attempt, resume_only=True)["code"], "SAVED_RESPONSE_HASH_MISMATCH")
            self.assertEqual(len(self.provider.calls), previous)
    def test_read_cost_conflict_is_not_silently_ignored(self):
        self.good_rows()
        with self.journal.locked():
            self.start(); self.review_file(); self.controller().review(self.attempt)
            self.review_file(amount="0.01")
            with self.assertRaisesRegex(SafetyError, "READ_COST_EVIDENCE_CONFLICT"): self.controller().review(self.attempt)
    def test_new_sample_waits_for_prior_quality_acceptance(self):
        with self.journal.locked():
            self.start()
            envelope = cli.prepare(CONFIG, "meta-two")
            atomic_json(self.root / "data/benchmarks/2026-10-03/meta-two/prepared.json", envelope)
            self.assertEqual(self.controller().run("meta-two")["code"], "PRIOR_SAMPLE_REVIEW_REQUIRED")
        self.assertEqual(sum(c[0] == "POST" for c in self.provider.calls), 1)
    def test_too_many_recent_runs_do_not_prove_a_unique_match(self):
        self.provider.lost = True
        with self.journal.locked():
            self.start(); self.provider.recovery_items = [{"id": "other"+str(n), "actId": "otherActor"} for n in range(101)]
            self.assertEqual(self.controller().run(self.attempt, resume_only=True)["code"], "RECOVERY_SEARCH_INCOMPLETE")
    def test_recovery_does_not_accept_a_different_input(self):
        self.provider.lost = True
        with self.journal.locked():
            self.start(); self.provider.recovery_items = [{"id": "runOne", "actId": ACTOR_ID}]
            self.provider.inputs["storerunOne"]["limitPerSource"] = 10
            self.assertEqual(self.controller().run(self.attempt, resume_only=True)["code"], "RECOVERY_UNIQUE_MATCH_NOT_ESTABLISHED")
    def test_newer_credential_file_requires_current_acknowledgement(self):
        self.facts["confirmed_at"] = (self.now-timedelta(days=1)).isoformat(); self.save_facts()
        with self.journal.locked(), self.assertRaisesRegex(SafetyError, "CREDENTIAL_CONFIRMATION_REQUIRED"): self.bind()
    def test_other_provider_edit_does_not_invalidate_bound_apify_acknowledgement(self):
        with self.journal.locked():
            original = self.bind()
            os.utime(self.root / "config/local/providers.env", (self.now.timestamp()+10, self.now.timestamp()+10))
            rebound = self.bind()
            self.assertEqual(original.generation, rebound.generation)
            self.assertEqual(original.namespace, rebound.namespace)
            self.assertEqual(self.journal.state()[0][original.namespace]["allowance"], "5")
    def test_changed_credential_cannot_reuse_previous_bound_acknowledgement(self):
        with self.journal.locked():
            original = self.bind()
            token_path = self.root / "config/local/providers.env"
            token_path.write_text("APIFY_TOKEN=fictional-different-key-1234567890\n")
            os.utime(token_path, (self.now.timestamp()+10, self.now.timestamp()+10))
            with self.assertRaisesRegex(SafetyError, "CREDENTIAL_CONFIRMATION_REQUIRED"): self.bind()
            self.assertEqual(load_binding(self.root / "config/local/apify_binding.json"), original)
    def test_unconfirmed_binding_cannot_bypass_newer_file_check(self):
        with self.journal.locked():
            original = self.bind()
            save_binding(self.root / "config/local/apify_binding.json", replace(original, owner_account_confirmed=False), self.journal)
            os.utime(self.root / "config/local/providers.env", (self.now.timestamp()+10, self.now.timestamp()+10))
            with self.assertRaisesRegex(SafetyError, "CREDENTIAL_CONFIRMATION_REQUIRED"): self.bind()
    def test_control_drift_cannot_be_healed_by_later_good_options(self):
        self.good_rows(); self.provider.status = "RUNNING"
        original = self.provider.open
        reads = 0
        def changed(request, timeout):
            nonlocal reads
            response = original(request, timeout)
            if urlsplit(request.full_url).path == "/v2/actor-runs/runOne":
                reads += 1
                if reads == 1:
                    self.provider.status, self.provider.cap = "SUCCEEDED", "0.02"
            return response
        with patch.object(self.provider, "open", side_effect=changed), self.journal.locked():
            self.start(); self.assertFalse(self.journal.state()[1][self.attempt]["controls_ok"])
            self.review_file()
            with self.assertRaisesRegex(SafetyError, "FUNCTIONAL_ACCEPTANCE_INCOMPLETE"): self.controller().review(self.attempt)
    def test_unsubmitted_reservation_can_be_deliberately_cancelled(self):
        with self.journal.locked():
            account = self.bind(); runner = self.controller(); runner.preflight(self.attempt)
            self.journal.reserve(runner.prepared, account, read_reserve="0.02")
            self.assertEqual(runner.resolve_non_submission(self.attempt)["code"], "UNSUBMITTED_RESERVATION_CANCELLED")
            self.assertEqual(self.journal.state()[1][self.attempt]["submission"], "not_submitted")
        self.assertFalse(any(c[0] == "POST" for c in self.provider.calls))
    def test_expired_period_allows_status_but_not_new_storage_charges(self):
        self.provider.status = "RUNNING"
        with self.journal.locked():
            self.start(); self.now += timedelta(days=31); self.provider.status = "SUCCEEDED"
            result = self.controller().run(self.attempt, resume_only=True)
            self.assertEqual(result["code"], "STORAGE_PERIOD_FUNDING_REVIEW_REQUIRED")
            self.assertEqual(self.journal.state()[1][self.attempt]["remote_status"], "SUCCEEDED")
            self.assertIsNotNone(self.journal.state()[1][self.attempt]["charge"])
        self.assertFalse(any(c[1].startswith("/v2/datasets/") for c in self.provider.calls))


class BoundaryTests(unittest.TestCase):
    def spec(self): return {"method": "GET", "path": "/v2/actor-runs", "query": {"limit": 50}, "body": None, "byte_limit": 65536, "token": TOKEN}
    def test_worker_timeout_is_real_and_narrow(self):
        started = time.monotonic()
        with self.assertRaisesRegex(HttpFailure, "HTTP_DEADLINE_EXCEEDED"):
            worker_call(self.spec(), timeout=0.2, command=[sys.executable, str(ROOT / "tests/fixtures/benchmarks/http_worker_fake.py"), "delay"])
        self.assertLess(time.monotonic()-started, 3)
    def test_worker_receives_secret_only_through_input(self):
        with patch.dict(os.environ, {"APIFY_TOKEN": "should-not-be-inherited", "HTTP_PROXY": "should-not-be-inherited"}):
            result = worker_call(self.spec(), command=[sys.executable, str(ROOT / "tests/fixtures/benchmarks/http_worker_fake.py"), "inspect"])
        value = decode_json(result.body)
        self.assertNotIn(TOKEN, " ".join(value["argv"]))
        self.assertNotIn("APIFY_TOKEN", value["environment"]); self.assertNotIn("HTTP_PROXY", value["environment"])
        self.assertFalse(Path(value["argv"][-1]).exists())
    def test_malformed_worker_result_is_rejected(self):
        with self.assertRaisesRegex(SafetyError, "HTTP_WORKER_FAILED"):
            worker_call(self.spec(), command=[sys.executable, str(ROOT / "tests/fixtures/benchmarks/http_worker_fake.py"), "malformed"])
    def test_reflected_key_in_selected_headers_is_not_saved(self):
        class Reflected:
            def open(self, request, timeout): return Response({}, {"retry-after": TOKEN})
        with self.assertRaisesRegex(SafetyError, "CREDENTIAL_REFLECTION_REFUSED"):
            perform_raw(self.spec(), Reflected())
    def test_worker_header_reflection_is_rejected_by_parent_too(self):
        with self.assertRaisesRegex(SafetyError, "HTTP_WORKER_FAILED"):
            worker_call(self.spec(), command=[sys.executable, str(ROOT / "tests/fixtures/benchmarks/http_worker_fake.py"), "header_reflection"])
    def test_http_query_cannot_contain_secret_or_unreviewed_flags(self):
        for query in ({"token": TOKEN}, {"waitForFinish": 3600}):
            with self.assertRaisesRegex(SafetyError, "INVALID_PROVIDER_REQUEST"): validate_spec({**self.spec(), "query": query})
    def test_duplicate_json_and_nonfinite_values_rejected(self):
        for raw in (b'{"id":1,"id":2}', b'{"usd":NaN}'):
            with self.assertRaisesRegex(SafetyError, "JSON_RESPONSE_INVALID"): decode_json(raw)
    def test_cost_values_are_exact_and_zero_with_events_is_unknown(self):
        self.assertEqual(actual_charge({"status": "FAILED", "usageTotalUsd": Decimal("0.000000001")}), "0.00000001")
        self.assertIsNone(actual_charge({"status": "SUCCEEDED", "usageTotalUsd": 0, "chargedEventCounts": {"ad": 1}}))
        for value in (True, Decimal("NaN"), "-1", None):
            self.assertIsNone(actual_charge({"status": "SUCCEEDED", "usageTotalUsd": value}))
    def test_creative_text_retained_and_unsafe_optional_links_omitted(self):
        result = map_meta_rows([{"ad_archive_id": "1", "snapshot": {"title": "=SUM(1,2)", "body": {"text": "<script>raw text</script>"},
                              "link_url": "javascript:alert(1)", "original_image_url": "https://user:pass@example.test/a"}}], purpose="discovery", query_reference="q")
        display = result["records"][0]["presentation"]
        self.assertEqual(display["title"], "=SUM(1,2)"); self.assertNotIn("link_url", display)
        self.assertNotIn("original_image_url", display)
    def test_cards_preserve_parent_identity_indices_and_flag_templates(self):
        result = map_meta_rows([{"ad_archive_id":"1", "page_name":" Fictional merchant ", "snapshot":{
            "title":"{{product.name}}", "cards":[None, {}, {"title":"Sink caddy", "body":{"text":"Product description"},
            "link_url":"https://example.test/product", "original_image_url":"https://example.test/image.jpg"}]}}],
            purpose="discovery", query_reference="fake")
        self.assertEqual(len(result["records"]), 1)
        row = result["records"][0]; self.assertEqual(row["advertiser_name"], "Fictional merchant")
        display = row["presentation"]
        self.assertEqual(display["unresolved_template_fields"], ["title"])
        self.assertEqual(display["invalid_card_indices"], [0])
        self.assertEqual(display["cards_total"], 3)
        self.assertEqual(display["cards"][0]["card_index"], 2)
        self.assertEqual(display["cards"][0]["body"], "Product description")
    def test_cards_display_bounds_do_not_destroy_raw_source(self):
        row = {"ad_archive_id":"1", "snapshot":{"cards":[{"title":"x"*2100} for _ in range(25)]}}
        original = json.dumps(row)
        display = map_meta_rows([row], purpose="discovery", query_reference="fake")["records"][0]["presentation"]
        self.assertEqual(len(display["cards"]), 20); self.assertIs(display["cards_truncated"], True)
        self.assertEqual(display["cards_total"], 25); self.assertEqual(len(display["cards"][0]["title"]), 2000)
        self.assertEqual(json.dumps(row), original)
    def test_unsafe_card_links_are_omitted_without_executing_text(self):
        row = {"ad_archive_id":"1", "snapshot":{"cards":[{"title":"<script>raw</script>", "link_url":"javascript:alert(1)",
            "original_image_url":"https://user:pass@example.test/a", "body":"=SUM(1,2)"}]}}
        card = map_meta_rows([row], purpose="discovery", query_reference="fake")["records"][0]["presentation"]["cards"][0]
        self.assertEqual(card["title"], "<script>raw</script>"); self.assertEqual(card["body"], "=SUM(1,2)")
        self.assertNotIn("link_url", card); self.assertNotIn("original_image_url", card)
    def test_invalid_card_container_is_explicit_but_parent_is_preserved(self):
        result = map_meta_rows([{"ad_archive_id":"1", "snapshot":{"cards":"not-a-list"}}], purpose="discovery", query_reference="fake")
        self.assertEqual(len(result["records"]), 1)
        self.assertIn("invalid_creative_cards", result["records"][0]["limitations"])
        self.assertEqual(result["status"], "partial")


if __name__ == "__main__": unittest.main()
