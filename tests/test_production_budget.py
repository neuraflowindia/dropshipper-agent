"""Production allocation rehearsals never access credentials or providers."""

from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.models import AccountBinding, SafetyError, PreparedMeta, as_production_request, prepared_request, utc_now
from product_radar.storage import Journal

spec = importlib.util.spec_from_file_location("budget_cli_fixture", ROOT / "scripts/benchmark_providers.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
CONFIG = json.loads((ROOT / "config/benchmarks/meta_first.json").read_text())


def request(attempt, scope="production"):
    original = PreparedMeta(**cli.prepare(CONFIG, attempt)["request"])
    return as_production_request(original) if scope == "production" else original


def binding(period="2026-10-period"):
    return AccountBinding("same-test-account", period, "benchmark", "generation-one", "a" * 64, utc_now(), True, True)


class ProductionBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.journal = Journal(Path(self.temporary.name) / "finance")
        self.account = binding()
        with self.journal.locked():
            self.journal.setup_budget(self.account.namespace, "5", production_provision="4")
            self.journal.adopt_production_scope(self.account.namespace, {"meta": "3", "amazon": "1"}, "Existing earmark partition for ordinary collection.", "approved-production")

    def reserve(self, attempt, scope="production", read="0.02"):
        return self.journal.reserve(request(attempt, scope), self.account, read_reserve=read)

    def terminal(self, attempt, charge="0.01"):
        self.journal.append("submit-" + attempt, "submitting", {"attempt": attempt})
        self.journal.append("receipt-" + attempt, "receipt", {"attempt": attempt, "run_id": "run-" + attempt, "dataset_id": "dataset-" + attempt})
        self.journal.append("terminal-" + attempt, "terminal", {"attempt": attempt, "status": "SUCCEEDED"})
        self.journal.append("charge-" + attempt, "charge", {"attempt": attempt, "usd": charge, "evidence": "public-charge-proof"})

    def test_old_trial_serialization_and_hash_unchanged(self):
        envelope = cli.prepare(CONFIG, "old-trial")
        old = PreparedMeta(**envelope["request"])
        self.assertEqual(old.to_dict(), envelope["request"])
        self.assertEqual(old.config_hash, envelope["config_hash"])
        self.assertNotIn("work_scope", old.to_dict())
        production = as_production_request(old)
        self.assertEqual(production.input, old.input)
        self.assertEqual(production.options, old.options)
        self.assertNotEqual(production.config_hash, old.config_hash)
        self.assertEqual(prepared_request(production.to_dict()).work_scope, "production")

    def test_existing_real_preparation_keeps_hash(self):
        paths = list((ROOT / "data/benchmarks").glob("*/meta-*/prepared.json"))
        for path in paths[:3]:
            envelope = json.loads(path.read_text())
            if "request" in envelope:
                self.assertEqual(prepared_request(envelope["request"]).config_hash, envelope["config_hash"])

    def test_one_reservation_event_transfers_not_double_counts(self):
        with self.journal.locked():
            before = self.journal.spendable(self.account.namespace)
            count = len(self.journal.events())
            event = self.reserve("production1")
            budget, attempts = self.journal.state()
            self.assertEqual(event["kind"], "reserved_production")
            self.assertEqual(len(self.journal.events()), count + 1)
            self.assertEqual(Decimal(budget[self.account.namespace]["policy"]["future_allocation"]), Decimal("3.95"))
            self.assertEqual(Decimal(budget[self.account.namespace]["production_scope"]["source_allocations"]["meta"]), Decimal("2.95"))
            self.assertEqual(self.journal.spendable(self.account.namespace), before)
            self.assertEqual(attempts["production1"]["read_hold"], "0.02")
            scope_event = next(e for e in self.journal.events() if e["kind"] == "production_scope")
            self.assertEqual(scope_event["data"]["source_allocations"]["meta"], "3")

    def test_policy_adoption_replay_is_idempotent_after_use(self):
        with self.journal.locked():
            self.reserve("production1")
            count = len(self.journal.events())
            self.journal.adopt_production_scope(self.account.namespace, {"meta": "3", "amazon": "1"}, "Existing earmark partition for ordinary collection.", "approved-production")
            self.assertEqual(len(self.journal.events()), count)
            self.assertEqual(Decimal(self.journal.state()[0][self.account.namespace]["production_scope"]["source_allocations"]["meta"]), Decimal("2.95"))

    def test_restart_replay_retains_atomic_transfer_and_old_prefix(self):
        with self.journal.locked():
            prefix = self.journal.path.read_bytes()
            self.reserve("production1")
            expected = self.journal.state()
            self.assertTrue(self.journal.path.read_bytes().startswith(prefix))
        recovered = Journal(self.journal.root)
        with recovered.locked():
            self.assertEqual(recovered.state(), expected)
            with self.assertRaisesRegex(SafetyError, "ATTEMPT_ALREADY_EXISTS"):
                recovered.reserve(request("production1"), self.account, read_reserve="0.02")
            self.assertEqual(recovered.state(), expected)

    def test_production_requires_adopted_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            journal = Journal(folder)
            with journal.locked():
                journal.setup_budget(self.account.namespace, "5", production_provision="4")
                with self.assertRaisesRegex(SafetyError, "PRODUCTION_SCOPE_NOT_ADOPTED"):
                    journal.reserve(request("not-adopted"), self.account)

    def test_source_allocation_cannot_create_funds(self):
        with self.journal.locked():
            with self.assertRaises(SafetyError):
                self.journal.adopt_production_scope(self.account.namespace, {"meta": "4.01"}, "Cannot mint funding", "overfunded")
            self.journal.adopt_production_scope(self.account.namespace, {"meta": "0.01"}, "Smaller bound", "small")
            with self.assertRaisesRegex(SafetyError, "PRODUCTION_ALLOCATION_EXHAUSTED"):
                self.reserve("too-large")

    def test_production_does_not_consume_lifetime_trial_stage_cap(self):
        with self.journal.locked():
            for n in range(8):
                self.reserve("production" + str(n), read="0")
            self.reserve("trial-after-production", scope="trial", read="0")
            self.assertNotIn("work_scope", self.journal.state()[1]["trial-after-production"])

    def test_trial_stage_caps_still_span_periods(self):
        with self.journal.locked():
            for n in range(4):
                self.reserve("trial" + str(n), scope="trial")
            next_account = binding("2026-11-period")
            self.journal.setup_budget(next_account.namespace, "5")
            with self.assertRaisesRegex(SafetyError, "BUDGET_EXHAUSTED"):
                self.journal.reserve(request("fifth-trial", "trial"), next_account, read_reserve="0.02")

    def test_source_allocation_does_not_reset_in_fifth_week(self):
        with self.journal.locked():
            self.journal.adopt_production_scope(self.account.namespace, {"meta": "0.05"}, "One funded attempt", "weekly-small")
            self.reserve("week-one")
            self.terminal("week-one", "0.03")
            self.journal.append("read-week-one", "read_cost", {"attempt": "week-one", "usd": "0.02", "evidence": "confirmed-reads"})
            for label in ("week-two", "week-four", "week-five"):
                with self.assertRaisesRegex(SafetyError, "PRODUCTION_ALLOCATION_EXHAUSTED"):
                    self.reserve(label)

    def test_unknown_actor_charge_does_not_release_funds(self):
        with self.journal.locked():
            self.reserve("unknown")
            self.assertIsNone(self.journal.settle_production_attempt("unknown"))
            self.assertEqual(self.journal.state()[1]["unknown"]["allocation_released"], "0")

    def test_known_actor_releases_unused_cap_but_unknown_reads_held(self):
        with self.journal.locked():
            self.reserve("settled")
            self.terminal("settled")
            event = self.journal.settle_production_attempt("settled")
            self.assertEqual(Decimal(event["data"]["usd"]), Decimal("0.02"))
            budget, attempts = self.journal.state()
            self.assertIsNone(attempts["settled"]["read_charge"])
            self.assertEqual(Decimal(budget[self.account.namespace]["policy"]["future_allocation"]), Decimal("3.97"))
            count = len(self.journal.events())
            self.assertIsNone(self.journal.settle_production_attempt("settled"))
            self.assertEqual(len(self.journal.events()), count)

    def test_final_read_settlement_releases_only_additional_unused_funds(self):
        with self.journal.locked():
            self.reserve("settled")
            self.terminal("settled")
            self.journal.settle_production_attempt("settled")
            self.journal.append("read-settled", "read_cost", {"attempt": "settled", "usd": "0.005", "evidence": "confirmed-reads"})
            event = self.journal.settle_production_attempt("settled")
            self.assertEqual(Decimal(event["data"]["usd"]), Decimal("0.015"))
            self.assertEqual(Decimal(self.journal.state()[1]["settled"]["allocation_released"]), Decimal("0.035"))

    def test_delayed_charge_consumes_headroom_after_release(self):
        with self.journal.locked():
            self.reserve("late")
            self.terminal("late", "0.005")
            self.journal.settle_production_attempt("late")
            before = self.journal.spendable(self.account.namespace)
            self.journal.append("late-charge", "charge_update", {"attempt": "late", "previous_usd": "0.005", "usd": "0.02", "evidence": "delayed-real-charge"})
            self.assertEqual(self.journal.spendable(self.account.namespace), before - Decimal("0.015"))
            self.assertIsNone(self.journal.settle_production_attempt("late"))

    def test_overrun_still_blocks_future_production(self):
        with self.journal.locked():
            self.reserve("overrun")
            self.terminal("overrun", "0.04")
            with self.assertRaisesRegex(SafetyError, "PROVIDER_CAP_OVERRUN_REVIEW_REQUIRED"):
                self.reserve("blocked")

    def test_current_available_is_not_a_fresh_allowance(self):
        with self.journal.locked():
            with self.assertRaisesRegex(SafetyError, "CURRENT_BALANCE_INSUFFICIENT"):
                self.journal.reserve(request("external-spend"), self.account, current_available="0.55", read_reserve="0.02")

    def test_cancelled_production_returns_unused_funding(self):
        with self.journal.locked():
            self.reserve("cancelled")
            self.journal.append("cancel-record", "cancelled", {"attempt": "cancelled"})
            self.journal.settle_production_attempt("cancelled")
            self.assertEqual(Decimal(self.journal.state()[0][self.account.namespace]["policy"]["future_allocation"]), Decimal("4"))

    def test_trial_and_production_share_account_period_headroom(self):
        with self.journal.locked():
            self.reserve("trial-paid", scope="trial")
            before = self.journal.spendable(self.account.namespace)
            self.reserve("production-funded")
            self.assertEqual(self.journal.spendable(self.account.namespace), before)

    def test_overfunded_earmark_cannot_hide_existing_trial_commitment(self):
        with self.journal.locked():
            self.reserve("trial-one", scope="trial")
            self.reserve("trial-two", scope="trial")
            self.journal.apply_policy(self.account.namespace, "4.50", "overearmarked")
            with self.assertRaisesRegex(SafetyError, "BUDGET_EXHAUSTED"):
                self.reserve("still-not-funded")


if __name__ == "__main__":
    unittest.main()
