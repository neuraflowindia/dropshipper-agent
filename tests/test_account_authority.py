"""Dated owner balances do not require daily approval; real authority remains."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.accounts import bind_account, load_handover
from product_radar.models import SafetyError
from product_radar.storage import Journal, atomic_json


class FakeTransport:
    def __init__(self, user_id="fictional-user", *, allowed=True):
        self.user_id = user_id
        self.allowed = allowed
        self.calls = []

    def clock(self):
        return 0

    def run_list(self):
        self.calls.append("read_runs")
        return {"data": {"items": []}} if self.allowed else {"data": {"not_items": []}}

    def identity(self):
        self.calls.append("read_identity")
        return {"data": {"id": self.user_id}}


class AccountAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # Never inspect a real credential or contact an external provider.
        atomic_json(self.root / "config/local/providers.env", {"synthetic_test_file": True})
        self.now = datetime.now(timezone.utc)
        self.path = self.root / "config/local/apify_handover.json"
        self.facts = {"schema_version": 2, "account_key": "owner-label", "period": "fictional-period",
            "period_ends_at": (self.now + timedelta(days=20)).isoformat(), "plan": "Free", "available_usd": "5",
            "balance_checked_at": (self.now - timedelta(days=5)).isoformat(), "independently_funded": True,
            "owner_account_confirmed": True, "owner_credential_confirmed": True,
            "confirmed_at": self.now.isoformat(), "provider_user_id": "fictional-user",
            "output_route_reviewed": True, "production_provision_usd": "0", "emergency_usd": "0"}
        self.save()

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        atomic_json(self.path, self.facts)

    def test_old_balance_is_accepted_unchanged_not_restamped_or_rewritten(self):
        original = self.path.read_bytes()
        result = load_handover(self.path, self.now)
        self.assertEqual(result, self.facts)
        self.assertEqual(result["balance_checked_at"], (self.now - timedelta(days=5)).isoformat())
        self.assertEqual(self.path.read_bytes(), original)

    def test_balance_and_authority_future_dates_still_block(self):
        for field in ("balance_checked_at", "confirmed_at"):
            with self.subTest(field=field):
                original = self.facts[field]
                self.facts[field] = (self.now + timedelta(seconds=1)).isoformat()
                self.save()
                with self.assertRaisesRegex(SafetyError, "HANDOVER_TIMESTAMP_FUTURE"):
                    load_handover(self.path, self.now)
                self.facts[field] = original

    def test_expired_period_blocks_even_with_genuine_saved_authority(self):
        self.facts["period_ends_at"] = self.now.isoformat()
        self.save()
        with self.assertRaisesRegex(SafetyError, "PROVIDER_PERIOD_EXPIRED"):
            load_handover(self.path, self.now)

    def test_missing_account_credential_or_route_authority_still_blocks(self):
        for field in ("owner_account_confirmed", "owner_credential_confirmed", "output_route_reviewed"):
            with self.subTest(field=field):
                self.facts[field] = False
                self.save()
                with self.assertRaisesRegex(SafetyError, "ACCOUNT_HANDOVER_REQUIRED"):
                    load_handover(self.path, self.now)
                self.facts[field] = True

    def test_no_new_required_fields_and_unknown_fields_are_not_silently_accepted(self):
        self.facts["invented_balance_verified"] = True
        self.save()
        with self.assertRaisesRegex(SafetyError, "HANDOVER_INVALID"):
            load_handover(self.path, self.now)

    def test_legacy_authority_is_preserved_without_a_credential_change(self):
        self.facts["schema_version"] = 1
        self.facts["owner_key_change_confirmed"] = self.facts.pop("owner_credential_confirmed")
        self.save()
        original = self.path.read_bytes()
        result = load_handover(self.path, self.now)
        self.assertTrue(result["owner_credential_confirmed"])
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(self.path.read_bytes(), original)

    def test_stale_balance_does_not_bypass_live_identity_or_access_checks(self):
        journal = Journal(self.root / "finance")
        for transport, reason in ((FakeTransport("different-user"), "ACCOUNT_IDENTITY_MISMATCH"),
                                  (FakeTransport(allowed=False), "ACCOUNT_ACCESS_RESPONSE_INVALID")):
            with self.subTest(reason=reason), journal.locked(), patch("product_radar.accounts.load_apify_token", return_value="fictional-key"):
                with self.assertRaisesRegex(SafetyError, reason):
                    bind_account(self.root, journal, lambda token: transport, self.now)
        with journal.locked():
            budgets, attempts = journal.state()
            self.assertEqual(budgets, {})
            self.assertEqual(attempts, {})

    def test_rebinding_stale_owner_balance_cannot_reset_the_existing_budget(self):
        journal = Journal(self.root / "finance")
        transport = FakeTransport()
        with journal.locked(), patch("product_radar.accounts.load_apify_token", return_value="fictional-key"):
            first = bind_account(self.root, journal, lambda token: transport, self.now)
            before = journal.state()
            self.facts["account_key"] = "renamed-owner-label"
            self.facts["available_usd"] = "4"
            self.save()
            second = bind_account(self.root, journal, lambda token: transport, self.now)
            self.assertEqual(first.namespace, second.namespace)
            self.assertEqual(journal.state(), before)
            self.assertEqual(journal.state()[0][first.namespace]["allowance"], "5")
        self.assertEqual(transport.calls, ["read_runs", "read_identity", "read_runs", "read_identity"])


if __name__ == "__main__":
    unittest.main()
