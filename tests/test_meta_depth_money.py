"""Money for the deeper Meta read (W11): allowance, months, notices, history.

Offline only: every Apify reply comes from the fictional provider; no key,
account, network or paid call is used.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from product_radar import meta_depth, meta_depth_money, spend_watch
from product_radar.accounts import bind_account, enable_account_list, load_account_registry
from product_radar.benchmark import MetaTrial, checked_pricing, load_preparation
from product_radar.meta_depth import ROUTE, attempt_id_for, prepared_for, save_preparation
from product_radar.meta_depth_money import MetaDepthWait, money_check, read_limits, renew_period, wait_for
from product_radar.models import PreparedResearch, SafetyError, as_production_request, digest
from product_radar.research_sources import AMAZON_CASES, RETIRED_AMAZON_CASES, SOURCES, prepare_source
from product_radar.storage import Journal, atomic_json, load_json
from test_meta_depth import CLOCK, LEGACY, REVIEW_AT, AccountProvider, DepthFixture


class AllowanceTests(DepthFixture):
    def setUp(self):
        super().setUp()
        self.free_read(); self.review()

    def spend(self, binding, attempt, charge, *, read="0.02"):
        """A settled earlier read in the same account period (ledger only)."""
        prepared = save_preparation(self.root, attempt, "earlier words", now=self.clock_at)
        with self.journal.locked():
            self.journal.reserve(prepared, binding, read_reserve="0.02")
            self.journal.append("submit-" + attempt, "submitting", {"attempt": attempt})
            self.journal.append("receipt-" + attempt, "receipt", {"attempt": attempt, "run_id": "run-" + attempt, "dataset_id": "data-" + attempt})
            self.journal.append("terminal-" + attempt, "terminal", {"attempt": attempt, "status": "SUCCEEDED"})
            self.journal.append("charge-" + attempt, "charge", {"attempt": attempt, "usd": charge, "evidence": "fictional-charge"})
            self.journal.append("capture-" + attempt, "capture", {"attempt": attempt, "hash": digest([attempt])})
            if read is not None:
                self.journal.append("read-" + attempt, "read_cost", {"attempt": attempt, "usd": read, "evidence": "fictional-read"})
            self.journal.settle_production_attempt(attempt)

    def hint(self, at=None):
        return self.handlers()["source_access_hint"]({"reconciling": False, "acquisition_id": "a-1", "request": self.task()["request"],
                                                      **({"as_of": at} if at else {})})

    def test_stop_at_the_protected_buffer_waits_for_the_next_allowance(self):
        # US$0.60 left: only US$0.10 sits above the protected US$0.50 buffer.
        self.handover.update(available_usd="0.60", production_provision_usd="0.10")
        binding = self.bind(allocation="0.10")
        self.spend(binding, "earlier-one", "0.03"); self.spend(binding, "earlier-two", "0.03")
        self.plan()
        result = self.execute()
        self.assertEqual(result["results"][0], {"task_id": result["results"][0]["task_id"], "state": "waiting",
            "actual_attempt": False, "reason": "META_DEPTH_ALLOWANCE_USED", "next_retry_at": "2026-11-02T00:00:00+00:00"})
        self.assertEqual((self.posts(), result["handler_calls"]), (0, 0))
        state = self.spend_state()
        self.assertEqual((state["waits"]["apify"]["reason"], state["waits"]["apify"]["next_retry_at"]),
                         ("META_DEPTH_ALLOWANCE_USED", "2026-11-02T00:00:00+00:00"))
        summary = spend_watch.allowance_summary(self.root, as_of=REVIEW_AT)
        self.assertEqual([row["route"] for row in summary["waits"]], ["apify"])
        self.assertIn("paused", [row["kind"] for row in summary["alerts"]])
        self.assertEqual((self.hint()["ready"], self.hint()["reason"]), (False, "META_DEPTH_ALLOWANCE_USED"))
        spend_watch.refresh_ledger_allowances(self.root, self.journal, as_of=REVIEW_AT)
        line = next(item for key, item in self.spend_state()["items"].items() if key.startswith("apify-"))
        self.assertEqual((line["percent"], line["flags"]), (100, [50, 75, 90, 100]))
        self.assertIn("wait for the next monthly allowance", line["text"])

    def test_live_usage_outside_the_ledger_stops_the_read(self):
        self.bind()
        self.provider.cycle("2026-10-03T00:00:00.000Z", "2026-11-01T23:59:59.999Z", used=4.9)  # spent elsewhere
        self.plan()
        result = self.execute()
        self.assertEqual((result["results"][0]["state"], result["results"][0]["reason"]), ("waiting", "META_DEPTH_ALLOWANCE_USED"))
        self.assertEqual((self.posts(), self.attempts()), (0, {}))

    def test_a_key_that_cannot_read_usage_is_a_notice_not_a_stop(self):
        self.bind()
        self.provider.limits_denied = True
        self.plan()
        self.assertEqual(self.execute()["results"][0]["state"], "acquired")
        notices = [row for row in spend_watch.allowance_summary(self.root, as_of=REVIEW_AT)["alerts"] if row["kind"] == "notice"]
        self.assertEqual(len(notices), 1)
        self.assertIn("may not read the account's monthly usage", notices[0]["message"])

    def test_a_paid_plan_is_refused(self):
        self.bind()
        self.provider.cycle("2026-10-03T00:00:00.000Z", "2026-11-01T23:59:59.999Z", used=0, maximum=29)
        self.plan()
        result = self.execute()
        self.assertEqual((result["results"][0]["reason"], self.posts()), ("APIFY_PAID_PLAN_REFUSED", 0))

    def test_the_new_month_is_recorded_automatically_despite_unsettled_read_holds(self):
        self.pool()
        self.handover.update(available_usd="0.60", production_provision_usd="0.05")
        binding = self.bind(allocation="0.05", pool=True)
        self.spend(binding, "earlier-one", "0.03", read=None)  # its read cost is still an unknown hold
        self.plan()
        self.assertEqual(self.execute()["results"][0]["reason"], "META_DEPTH_ALLOWANCE_USED")
        self.clock_at = datetime(2026, 11, 2, 1, 0, tzinfo=timezone.utc)
        # Apify has not started a new cycle yet: a calm renewal wait.
        self.provider.cycle("2026-10-03T00:00:00.000Z", "2026-11-01T23:59:59.999Z", used=0.6)
        self.assertIsNone(self.hint("2026-11-02T01:00:00+00:00")["reason"])  # the handler records the month itself
        with self.assertRaises(MetaDepthWait) as wait:
            self.handlers()["handler"](self.context("acq-renewal", self.task()["request"]))
        self.assertEqual(str(wait.exception), "META_DEPTH_PERIOD_RENEWAL_PENDING")
        self.assertEqual(load_json(self.root / "config/local/apify_handover.json")["period"], "period-one")
        # The new cycle has started: recorded, earmarked and used with free reads only.
        self.provider.cycle("2026-11-02T00:00:00.000Z", "2026-12-01T23:59:59.999Z", used=0)
        self.clock_at = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
        result = self.execute(as_of="2026-11-02T08:00:00+00:00")
        self.assertEqual(result["results"][0]["state"], "acquired", result)
        handover = load_json(self.root / "config/local/apify_handover.json")
        self.assertEqual((handover["period"], handover["period_ends_at"], handover["available_usd"],
                          handover["production_provision_usd"], handover["confirmed_at"]),
                         ("2026-11-02_2026-12-02", "2026-12-02T00:00:00+00:00", "5.00000000", "4.45", self.handover["confirmed_at"]))
        attempt = self.attempts()[attempt_id_for(self.task()["acquisition_id"])]
        self.assertTrue(attempt["namespace"].endswith(":2026-11-02_2026-12-02"))
        with self.journal.locked():
            budget = self.journal.state()[0][attempt["namespace"]]
        self.assertEqual(budget["production_scope"]["decision"], "auto-2026-11-02_2026-12-02")
        # 4.45 earmarked (the US$5 month less 0.55), less this read's charge and its held read cost.
        self.assertEqual(Decimal(budget["production_scope"]["source_allocations"]["meta"]), Decimal("4.4262"))
        self.assertIsNone(self.attempts()["earlier-one"]["read_charge"])  # the old hold stays on its own month
        self.assertEqual(self.posts(), 1)
        state = self.spend_state()
        self.assertNotIn("apify", state["waits"])
        lines = [item for key, item in state["items"].items() if key.startswith("apify-")]
        self.assertEqual([line["details"]["period"] for line in lines], ["2026-11-02_2026-12-02"])

    def test_without_the_account_list_the_month_end_becomes_an_owner_notice(self):
        self.handover.update(period_ends_at="2026-10-09T00:00:00+00:00")
        self.bind(); self.plan()
        self.clock_at = datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc)
        self.provider.cycle("2026-10-09T00:00:00.000Z", "2026-11-08T23:59:59.999Z", used=0)
        hint = self.hint("2026-10-09T01:00:00+00:00")
        self.assertEqual((hint["reason"], hint["next_retry_at"]), ("META_DEPTH_PERIOD_RENEWAL_PENDING", "2026-10-09T07:00:00+00:00"))
        context = {"reconciling": False, "acquisition_id": "a-1", "request": self.task()["request"]}
        self.assertEqual(self.handlers()["source_access_preflight"](context)["reason"], "META_DEPTH_PERIOD_RENEWAL_PENDING")
        self.assertEqual(self.spend_state()["waits"]["apify"]["reason"], "META_DEPTH_PERIOD_RENEWAL_PENDING")
        with self.assertRaisesRegex(SafetyError, "META_DEPTH_ACCOUNT_POOL_REQUIRED"):
            renew_period(self.root, self.journal, load_account_registry(self.root), self.provider.factory, now=self.clock_at)
        for hours in (1, 8):
            self.clock_at = datetime(2026, 10, 9, hours, 0, tzinfo=timezone.utc)
            with self.assertRaises(MetaDepthWait) as wait:
                self.handlers()["handler"](self.context("acq-month-end", self.task()["request"]))
            self.assertEqual(str(wait.exception), "META_DEPTH_ACCOUNT_POOL_REQUIRED")
        notices = [row for row in spend_watch.allowance_summary(self.root, as_of="2026-10-09T09:00:00+00:00")["alerts"]
                   if row["kind"] == "notice"]
        self.assertEqual([row["message"][:39] for row in notices], ["The Apify month has ended and the accou"])
        self.assertEqual(self.posts(), 0)

    def test_waits_lasting_six_hours_become_one_plain_notice(self):
        self.plan()
        preflight = self.handlers()["source_access_preflight"]
        context = {"reconciling": False, "acquisition_id": "a-1", "request": self.task()["request"]}
        with patch("product_radar.meta_depth.money_check", side_effect=MetaDepthWait("PRODUCTION_SCOPE_NOT_ADOPTED",
                                                                                    next_retry_at="2026-10-08T12:00:00+00:00")):
            for hours in (0, 3, 7, 9):
                self.clock_at = CLOCK + timedelta(hours=hours)
                self.assertEqual(preflight(context)["reason"], "PRODUCTION_SCOPE_NOT_ADOPTED")
        notices = [row for row in spend_watch.allowance_summary(self.root, as_of="2026-10-08T21:00:00+00:00")["alerts"]
                   if row["kind"] == "notice"]
        self.assertEqual(len(notices), 1)
        self.assertIn("has not been set aside", notices[0]["message"])
        self.assertEqual(self.spend_state()["waits"], {})  # not a money pause
        self.bind()
        self.clock_at = CLOCK + timedelta(hours=10)
        self.assertEqual(preflight(context), {"action": "collect"})
        self.assertIn("apify_hold", self.spend_state()["money"])  # remembered until a read is admitted
        self.assertEqual(self.execute(as_of="2026-10-08T21:00:00+00:00")["results"][0]["state"], "acquired")
        self.assertNotIn("apify_hold", self.spend_state()["money"])

    def test_account_problems_are_calm_specific_waits(self):
        self.plan()
        result = self.execute()  # no saved account files: nothing can be reserved
        self.assertEqual((result["results"][0]["state"], result["results"][0]["reason"]), ("waiting", "PRIVATE_BINDING_UNAVAILABLE"))
        self.assertEqual(result["results"][0]["next_retry_at"], "2026-10-08T13:00:00+00:00")  # an hour after the check
        disabled = {"pool_enabled": True, "accounts": [dict(LEGACY["accounts"][0], id="spare", enabled=False)]}
        prepared, _ = prepared_for("meta-depth-x", "words")
        with self.assertRaises(MetaDepthWait) as wait:
            money_check(self.root, self.journal, disabled, prepared, now=self.clock_at)
        self.assertEqual(str(wait.exception), "ACCOUNT_PROFILE_NOT_CONFIGURED")

    def test_controller_refusals_before_reservation_are_waits(self):
        self.bind(); self.plan()
        self.provider.actor["data"]["id"] = "changed-actor"  # reviewed public metadata changed
        result = self.execute()
        self.assertEqual((result["results"][0]["state"], result["results"][0]["reason"]),
                         ("waiting", "PUBLIC_METADATA_CHANGED_REVIEW_REQUIRED"))
        self.assertEqual((self.posts(), self.attempts()), (0, {}))
        self.provider.actor["data"]["id"] = AccountProvider().actor["data"]["id"]
        with patch.object(MetaTrial, "run", return_value={"verdict": "Blocked", "code": "PREFLIGHT_INCOMPLETE"}):
            later = self.execute(as_of="2026-10-08T13:30:00+00:00")
        self.assertEqual((later["results"][0]["state"], later["results"][0]["reason"]), ("waiting", "PREFLIGHT_INCOMPLETE"))

    def test_busy_ledger_is_retried_soon_not_a_long_wait(self):
        self.bind(); self.plan()
        with self.journal.locked():
            with self.assertRaisesRegex(SafetyError, "NESTED_LOCK_REFUSED|RUNNER_ALREADY_ACTIVE"):
                self.handlers()["source_access_preflight"]({"reconciling": False, "acquisition_id": "a-1",
                                                             "request": self.task()["request"]})
        with patch("product_radar.storage.Journal.check_reservation", side_effect=SafetyError("RUNNER_ALREADY_ACTIVE")):
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                money_check(self.root, self.journal, LEGACY, prepared_for("meta-depth-y", "words")[0], now=self.clock_at)
            with self.assertRaisesRegex(SafetyError, "RUNNER_ALREADY_ACTIVE"):
                self.handlers()["handler"](self.context("acq-busy", self.task()["request"]))

    def test_spend_watch_problems_never_block_the_read(self):
        self.bind(); self.plan()
        with patch("product_radar.spend_watch.refresh_ledger_allowances", side_effect=SafetyError("RUNNER_ALREADY_ACTIVE")):
            self.assertEqual(self.execute()["results"][0]["state"], "acquired")


class AccountMonthTests(unittest.TestCase):
    def test_limits_are_read_exactly_and_refuse_paid_plans(self):
        now = datetime(2026, 10, 8, tzinfo=timezone.utc)
        class Transport:
            def __init__(self, payload): self.payload = payload
            def limits(self):
                if isinstance(self.payload, Exception): raise self.payload
                return self.payload
        good = {"data": {"monthlyUsageCycle": {"startAt": "2026-10-03T00:00:00.000Z", "endAt": "2026-11-01T23:59:59.999Z"},
                         "limits": {"maxMonthlyUsageUsd": Decimal("5")}, "current": {"monthlyUsageUsd": Decimal("1.234")}}}
        self.assertEqual(read_limits(Transport(good), now=now), {"period": "2026-10-03_2026-11-02",
            "period_ends_at": "2026-11-02T00:00:00+00:00", "max_usd": "5", "used_usd": "1.234", "available_usd": "3.76600000"})
        from product_radar.http_io import HttpFailure
        for payload, code in ((HttpFailure("HTTP_STATUS", status=403), "APIFY_LIMITS_READ_DENIED"),
                              ({"data": {}}, "APIFY_LIMITS_RESPONSE_INVALID"),
                              ({**good, "data": {**good["data"], "limits": {"maxMonthlyUsageUsd": Decimal("49")}}}, "APIFY_PAID_PLAN_REFUSED"),
                              ({**good, "data": {**good["data"], "current": {"monthlyUsageUsd": True}}}, "APIFY_LIMITS_RESPONSE_INVALID"),
                              ({**good, "data": {**good["data"], "monthlyUsageCycle": {"startAt": "2026-11-02T00:00:00Z",
                                                                                      "endAt": "2026-12-01T23:59:59Z"}}}, "APIFY_LIMITS_CYCLE_INVALID")):
            with self.subTest(code=code), self.assertRaisesRegex(SafetyError, code):
                read_limits(Transport(payload), now=now)
        with self.assertRaisesRegex(Exception, "HTTP_DEADLINE"):
            read_limits(Transport(HttpFailure("HTTP_DEADLINE_EXCEEDED", retryable=True)), now=now)

    def test_allocation_settings_are_validated(self):
        settings = meta_depth_money.load_allocation()
        self.assertEqual((settings["reserve_usd"], settings["sources"]), ("0.55", {"meta": "4.45"}))
        with tempfile.TemporaryDirectory() as directory:
            for change in ({"reserve_usd": "0.50"}, {"sources": {}}, {"sources": {"unknown": "1"}}, {"kind": "other"}):
                path = Path(directory) / "allocation.json"
                atomic_json(path, dict(settings, **change))
                with self.subTest(change=change), self.assertRaisesRegex(SafetyError, "APIFY_ALLOCATION_SETTINGS_INVALID"):
                    meta_depth_money.load_allocation(path)

    def test_wait_codes(self):
        now = datetime(2026, 10, 8, tzinfo=timezone.utc)
        for code, reason, at in (("BUDGET_EXHAUSTED", "META_DEPTH_ALLOWANCE_USED", "2026-11-02T00:00:00+00:00"),
                                 ("PROVIDER_PERIOD_EXPIRED", "META_DEPTH_PERIOD_RENEWAL_PENDING", "2026-10-08T06:00:00+00:00"),
                                 ("PRIOR_PRODUCTION_CONTROLS_UNRESOLVED", "META_DEPTH_EARLIER_RUN_UNSETTLED", "2026-10-08T00:10:00+00:00"),
                                 ("APIFY_LIMITS_READ_DENIED", "APIFY_LIMITS_READ_DENIED", "2026-10-08T06:00:00+00:00"),
                                 ("PRODUCTION_SCOPE_NOT_ADOPTED", "PRODUCTION_SCOPE_NOT_ADOPTED", "2026-10-08T01:00:00+00:00")):
            wait = wait_for(code, now=now, period_ends_at="2026-11-02T00:00:00+00:00")
            self.assertEqual((str(wait), wait.next_retry_at), (reason, at))
        self.assertEqual(wait_for("CURRENT_BALANCE_INSUFFICIENT", now=now).next_retry_at, "2026-10-08T01:00:00+00:00")


class AccountListTests(DepthFixture):
    def test_cutover_writes_only_the_funded_account(self):
        with self.assertRaisesRegex(SafetyError, "ACCOUNT_FILES_REQUIRED"):
            enable_account_list(self.root)
        self.bind()
        self.assertEqual(enable_account_list(self.root), {"written": True, "path": "config/local/apify_accounts.json"})
        registry = load_account_registry(self.root)
        self.assertEqual((registry["pool_enabled"], [row["id"] for row in registry["accounts"]]), (True, ["main"]))
        self.assertEqual(enable_account_list(self.root)["written"], False)  # repeating it is harmless
        atomic_json(self.root / "config/local/apify_accounts.json", dict(registry, accounts=[dict(registry["accounts"][0], id="other")]))
        with self.assertRaisesRegex(SafetyError, "ACCOUNT_REGISTRY_EXISTS"):
            enable_account_list(self.root)

    def test_same_key_month_renewal_ignores_read_holds_but_key_changes_do_not(self):
        binding = self.bind()
        prepared = save_preparation(self.root, "held", "earlier words", now=self.clock_at)
        with self.journal.locked():
            self.journal.reserve(prepared, binding, read_reserve="0.02")
            self.journal.append("submit-held", "submitting", {"attempt": "held"})
            self.journal.append("receipt-held", "receipt", {"attempt": "held", "run_id": "run-held", "dataset_id": "data-held"})
            self.journal.append("terminal-held", "terminal", {"attempt": "held", "status": "SUCCEEDED"})
            self.journal.append("charge-held", "charge", {"attempt": "held", "usd": "0.01", "evidence": "fictional-charge"})
        self.clock_at = datetime(2026, 11, 2, 1, tzinfo=timezone.utc)
        self.handover.update(period="period-two", period_ends_at="2026-12-02T00:00:00+00:00",
                             balance_checked_at=self.clock_at.isoformat())
        atomic_json(self.root / "config/local/apify_handover.json", self.handover)
        with self.journal.locked():
            renewed = bind_account(self.root, self.journal, self.provider.factory, self.clock_at)
        self.assertEqual((renewed.period, renewed.generation), ("period-two", binding.generation))
        # A different key is still refused while the old month has unsettled holds.
        from dataclasses import replace
        from product_radar.accounts import save_binding
        path = self.root / "config/local/apify_binding.json"
        with self.journal.locked(), self.assertRaisesRegex(SafetyError, "OLD_ACCOUNT_RECOVERY_REQUIRED"):
            save_binding(path, replace(renewed, token_digest="b" * 64, generation="key-other"), self.journal)
        # A finished job whose bill is unknown still blocks even a same-key new month.
        with self.journal.locked():
            self.journal.adopt_production_scope(renewed.namespace, {"meta": "1"}, "Fictional earmark.", "fictional-month-two")
            prepared = save_preparation(self.root, "unknown-bill", "other words", now=self.clock_at)
            self.journal.reserve(prepared, renewed, read_reserve="0.02")
            self.journal.append("submit-u", "submitting", {"attempt": "unknown-bill"})
            self.journal.append("receipt-u", "receipt", {"attempt": "unknown-bill", "run_id": "run-u", "dataset_id": "data-u"})
            self.journal.append("terminal-u", "terminal", {"attempt": "unknown-bill", "status": "SUCCEEDED"})
            with self.assertRaisesRegex(SafetyError, "OLD_ACCOUNT_RECOVERY_REQUIRED"):
                save_binding(path, replace(renewed, period="period-three"), self.journal)


class HistoricalLedgerTests(unittest.TestCase):
    """Retired Movers & Shakers jobs stay readable; they are never prepared or launched again."""

    def movers(self, attempt, case="kitchen-movers-and-shakers"):
        spec = SOURCES["amazon"]
        url = RETIRED_AMAZON_CASES[case]
        return PreparedResearch(attempt, spec["actor"], spec["build"], spec["build_id"], url.split("/")[4],
            {"urls": [url], "subcategoryLevel": 0, "nextPage": False, "retries": 3},
            {"build": spec["build"], "timeout": 300, "maxTotalChargeUsd": spec["cap"], "restartOnError": "false"},
            spec["cap"], "2026-10-04T00:00:00+00:00", spec["metadata_hash"])

    def test_saved_retired_preparation_loads_but_is_never_new_or_launched(self):
        self.assertNotIn("kitchen-movers-and-shakers", AMAZON_CASES)
        with self.assertRaisesRegex(SafetyError, "AMAZON_CASE_REQUIRED"):
            prepare_source("amazon", "kitchen-movers-and-shakers", "amazon-new", "2026-10-14T00:00:00+00:00")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = self.movers("amazon-old")
            atomic_json(root / "data/benchmarks/2026-10-04/amazon-old/prepared.json",
                        {"request": request.to_dict(), "config_hash": request.config_hash, "execution_mode": "offline_preparation"})
            self.assertEqual(load_preparation(root, "amazon-old").input["urls"], [RETIRED_AMAZON_CASES["kitchen-movers-and-shakers"]])
        with self.assertRaisesRegex(SafetyError, "AMAZON_CASE_RETIRED"):
            checked_pricing({}, {}, request)

    def test_earlier_retired_jobs_do_not_stop_billing_checks(self):
        from test_production_budget import binding
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = Journal(root / "data/benchmarks/state/apify")
            account = binding()
            request = as_production_request(self.movers("amazon-movers"))
            atomic_json(root / "data/benchmarks/2026-10-04/amazon-movers/prepared.json",
                        {"request": request.to_dict(), "config_hash": request.config_hash, "execution_mode": "offline_preparation"})
            with journal.locked():
                journal.setup_budget(account.namespace, "5", production_provision="4")
                journal.adopt_production_scope(account.namespace, {"amazon": "1"}, "Fictional earmark.", "fictional-amazon")
                journal.reserve(request, account, stage="amazon", read_reserve="0.02")
                journal.append("submit", "submitting", {"attempt": "amazon-movers"})
                journal.append("receipt", "receipt", {"attempt": "amazon-movers", "run_id": "run-movers", "dataset_id": "data-movers"})
                journal.append("terminal", "terminal", {"attempt": "amazon-movers", "status": "SUCCEEDED"})
                journal.append("controls", "controls", {"attempt": "amazon-movers", "ok": True, "evidence": "fictional-controls"})
                journal.append("charge", "charge", {"attempt": "amazon-movers", "usd": "0.002", "evidence": "fictional-charge"})
                MetaTrial(root, journal).check_previous_reviews(journal.state()[1])  # no UNREVIEWED_INPUT


if __name__ == "__main__":
    unittest.main()
