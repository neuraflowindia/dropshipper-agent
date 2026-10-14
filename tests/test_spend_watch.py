"""Spend-watch core: thresholds, once-only flags, rollover, Apify, money, meter, backups.

Synthetic temporary stores and ledgers only; no provider, model or real store.
"""

from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from product_radar import spend_watch
from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory, TABLES
from product_radar.runtime_storage import runtime_path
from product_radar.spend_watch import (allowance_summary, apify_usage, load_settings, load_state, money_route_wait,
                                       observe, refresh_ledger_allowances, validate_settings, writing)
from product_radar.storage import Journal

NOW = "2026-10-14T10:00:00+00:00"
NEXT_MONTH = "2026-11-03T10:00:00+00:00"
ACCOUNT = "apify-0123456789abcdef01234567"
PERIOD = ACCOUNT + ":2026-10-period"


class FakeJournal:
    """The ledger reader contract only: events and the reduced state."""
    def __init__(self, events, financial, *, locked=False):
        self._events, self._financial, self._locked, self.lock_calls = events, financial, locked, 0

    @contextmanager
    def locked(self, *, timeout=0):
        self.lock_calls += 1
        yield self

    def events(self):
        return deepcopy(self._events)

    def state(self):
        return deepcopy(self._financial)


def ledger(*caps, allowance="5.00", at=NOW, namespace=PERIOD):
    events = [{"kind": "budget", "at": at, "data": {"namespace": namespace}}]
    attempts = {}
    for index, cap in enumerate(caps):
        attempt = "attempt-%d" % index
        events.append({"kind": "reserved", "at": at, "data": {"attempt": attempt}})
        attempts[attempt] = {"namespace": namespace, "cap": cap, "charge": None, "read_charge": None, "read_hold": "0"}
    return events, ({namespace: {"allowance": allowance}}, attempts)


class SpendWatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="spend-watch-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = load_settings()

    def alerts(self):
        return allowance_summary(self.root, as_of=NOW, settings=self.settings, alert_limit=200)["alerts"]

    def observe(self, used, *, period="p1", at=NOW, key="item"):
        with writing(self.root, self.settings) as (state, alerts):
            observe(state, alerts, key, label="Test allowance", period=period, used=used, allowance=100,
                    unit="credits", at=at, text="Test allowance line.")
        return load_state(self.root)["items"][key]

    def test_repository_settings_are_valid_and_strict(self):
        settings = load_settings()
        self.assertEqual(settings["money"]["monthly_target_inr"], "750")
        self.assertEqual([p["credits"] for p in settings["quickcommerce"]["packs"]], [2500, 10000, 50000])
        broken = []
        for change in (lambda s: s["money"].update(review_above_inr="100"),
                       lambda s: s["money"].update(usd_inr_rate="0"),
                       lambda s: s["money"].update(timezone="UTC"),
                       lambda s: s["quickcommerce"]["packs"].reverse(),
                       lambda s: s["quickcommerce"]["packs"][0].update(price_inr="5.001"),
                       lambda s: s["quickcommerce"].update(prices_checked_on="soon"),
                       lambda s: s["hosting"].update(provider_polling=True),
                       lambda s: s.update(extra=True), lambda s: s.pop("money")):
            value = deepcopy(settings); change(value); broken.append(value)
        for value in broken + [None, []]:
            with self.subTest(value=value), self.assertRaisesRegex(SafetyError, "SPEND_WATCH_SETTINGS_INVALID"):
                validate_settings(value)

    def test_thresholds_flag_once_per_period_and_roll_over(self):
        self.assertEqual(self.observe(49)["flags"], [])
        self.assertEqual(self.observe(50)["flags"], [50])
        self.assertEqual(self.observe(91)["flags"], [50, 75, 90])
        self.assertEqual(self.observe(60)["flags"], [50, 75, 90])  # falling back does not re-arm
        self.observe(95)
        self.assertEqual([(a["threshold"], a["percent"]) for a in self.alerts()], [(50, 50), (75, 91), (90, 91)])
        self.assertEqual(self.observe(100)["flags"], list(spend_watch.THRESHOLDS))
        rolled = self.observe(80, period="p2")
        self.assertEqual(rolled["flags"], [50, 75])
        rows = self.alerts()
        self.assertEqual(len(rows), 6)
        self.assertEqual(len({row["alert_id"] for row in rows}), 6)
        self.assertEqual(rows[-1]["message"], "Test allowance: 80% used (passed 75%).")

    def test_alert_log_is_dated_in_owner_time_and_summary_filters(self):
        self.observe(55, at="2026-10-14T19:00:00+00:00")  # 15 October in India
        logs = sorted(p.name for p in runtime_path(self.root, spend_watch.AREA, "alerts").iterdir())
        self.assertEqual(logs, ["2026-10-15.jsonl"])
        self.assertEqual(allowance_summary(self.root, alerts_after="2026-10-14T20:00:00+00:00")["alerts"], [])
        self.assertEqual(len(allowance_summary(self.root, alerts_after="2026-10-14T19:00:00+00:00")["alerts"]), 1)
        for bad in ({"alerts_after": "yesterday"}, {"alert_limit": 0}, {"alert_limit": True}):
            with self.subTest(bad=bad), self.assertRaises(SafetyError):
                allowance_summary(self.root, **bad)

    def test_summary_is_read_only_and_lists_hosting_without_records(self):
        summary = allowance_summary(self.root, as_of=NOW)
        self.assertEqual(summary["state"], "not_recorded")
        self.assertEqual([line["item"] for line in summary["lines"]], ["hosting"])
        self.assertIn("US$1 Cloudflare Budget Alert", summary["lines"][0]["text"])
        self.assertEqual(summary["waits"], [])
        self.assertFalse((self.root / "data").exists())
        self.assertIsNone(money_route_wait(self.root, "quickcommerce"))
        for route in ("apify", "amazon"):  # Apify's own ledger is its gate; no saved wait here
            with self.assertRaisesRegex(SafetyError, "SPEND_WATCH_ROUTE_INVALID"):
                money_route_wait(self.root, route)

    def test_tampered_or_bad_state_and_logs_fail_closed(self):
        self.observe(55)
        path = runtime_path(self.root, spend_watch.AREA, "state.json")
        wrapper = json.loads(path.read_text())
        wrapper["state"]["items"]["item"]["flags"] = []
        path.write_text(json.dumps(wrapper))
        with self.assertRaisesRegex(SafetyError, "SPEND_WATCH_STATE_INVALID"):
            load_state(self.root)
        wrapper["hash"] = digest(wrapper["state"]); wrapper["state"]["waits"]["amazon"] = {"reason": "X", "since": NOW}
        wrapper["hash"] = digest(wrapper["state"]); path.write_text(json.dumps(wrapper))
        with self.assertRaisesRegex(SafetyError, "SPEND_WATCH_STATE_INVALID"):
            load_state(self.root)
        path.unlink()
        log = next(runtime_path(self.root, spend_watch.AREA, "alerts").iterdir())
        log.write_text('{"no_id": true}\n')
        with self.assertRaisesRegex(SafetyError, "SPEND_WATCH_ALERT_LOG_INVALID"):
            allowance_summary(self.root)
        log.write_bytes(b"x" * (spend_watch.MAX_ALERT_FILE_BYTES + 1))
        with self.assertRaisesRegex(SafetyError, "SPEND_WATCH_ALERT_LOG_TOO_LARGE"):
            allowance_summary(self.root)

    def test_failed_change_saves_nothing(self):
        with self.assertRaises(RuntimeError):
            with writing(self.root, self.settings) as (state, alerts):
                observe(state, alerts, "item", label="X", period="p", used=90, allowance=100, unit="credits",
                        at=NOW, text="X.")
                raise RuntimeError("interrupted")
        self.assertIsNone(load_state(self.root))
        with self.assertRaisesRegex(SafetyError, "SPEND_WATCH_ALERT_INVALID"):
            spend_watch.alert([], item="x", label="x", kind="other", at=NOW, message="x")
        with self.assertRaisesRegex(SafetyError, "SPEND_WATCH_ROUTE_INVALID"):
            spend_watch.set_wait(spend_watch._empty(), [], "amazon", reason="X", at=NOW, label="x", message="x")

    def test_apify_usage_counts_holds_per_current_period_and_month(self):
        events, financial = ledger("0.20", "0.20")
        financial[1]["attempt-1"].update(charge="0.10", read_charge=None, read_hold="0.02")
        events.append({"kind": "budget", "at": NOW, "data": {"namespace": ACCOUNT + ":old-period"}})
        events.append({"kind": "budget", "at": NOW, "data": {"namespace": PERIOD}})
        financial[0][ACCOUNT + ":old-period"] = {"allowance": "5"}
        financial[1]["attempt-0"]["renewed_reads"] = {"read-1": {"read_id": "read-1", "namespace": PERIOD,
                                                                 "charge": None, "cap": "0.02"}}
        events.append({"kind": "renewed_read_reserved", "at": NEXT_MONTH, "data": {"attempt": "attempt-0", "read_id": "read-1"}})
        usage = apify_usage(events, financial, self.settings)
        self.assertEqual([(row["period"], row["used_usd"], row["usable_usd"]) for row in usage["current"]],
                         [("2026-10-period", Decimal("0.34"), Decimal("4.50"))])
        self.assertEqual(usage["by_month"], {"2026-10": "0.32", "2026-11": "0.02"})
        events, financial = ledger("0.20", allowance="0.40")
        self.assertEqual(apify_usage(events, financial, self.settings)["current"][0]["usable_usd"], Decimal("0"))
        with self.assertRaisesRegex(SafetyError, "SPEND_WATCH_LEDGER_INVALID"):
            apify_usage(events[:1], financial, self.settings)
        financial[1]["attempt-0"]["renewed_reads"] = {"r": {"read_id": "r", "namespace": PERIOD, "charge": "0.01", "cap": "0.02"}}
        with self.assertRaisesRegex(SafetyError, "SPEND_WATCH_LEDGER_INVALID"):
            apify_usage(events, financial, self.settings)

    def test_apify_full_allowance_is_flagged_and_rolls_over_with_a_new_period(self):
        events, financial = ledger("0.20", *["0.42"] * 10)  # 4.40 of 4.50
        journal = FakeJournal(events, financial)
        result = refresh_ledger_allowances(self.root, journal, as_of=NOW, settings=self.settings)
        self.assertEqual(result["apify_periods"], 1)
        self.assertEqual(journal.lock_calls, 1)
        item = next(v for k, v in load_state(self.root)["items"].items() if k.startswith("apify-"))
        self.assertEqual((item["percent"], item["flags"]), (97, [50, 75, 90]))
        self.assertEqual(item["text"], "Apify free allowance: US$4.40 of US$4.50 used (97%).")
        self.assertNotIn(ACCOUNT, json.dumps(load_state(self.root)))
        events, financial = ledger("0.20", *["0.42"] * 10, "0.20")
        refresh_ledger_allowances(self.root, FakeJournal(events, financial, locked=True), as_of=NOW, settings=self.settings)
        item = next(v for k, v in load_state(self.root)["items"].items() if k.startswith("apify-"))
        self.assertEqual(item["flags"], [50, 75, 90, 100])
        self.assertIn("Apify reads wait for the next monthly allowance", item["text"])
        self.assertEqual(load_state(self.root)["waits"], {})  # display only: the ledger refuses the reservation
        refresh_ledger_allowances(self.root, FakeJournal(events, financial), as_of=NOW, settings=self.settings)
        self.assertEqual([a["threshold"] for a in self.alerts() if a["item"].startswith("apify-")], [50, 75, 90, 100])
        events, financial = ledger("0.20", namespace=ACCOUNT + ":2026-11-period", at=NEXT_MONTH)
        refresh_ledger_allowances(self.root, FakeJournal(events, financial), as_of=NEXT_MONTH, settings=self.settings)
        items = [v for k, v in load_state(self.root)["items"].items() if k.startswith("apify-")]
        self.assertEqual([(item["percent"], item["flags"]) for item in items], [(4, [])])

    def test_desktop_spend_lines_and_recent_alerts(self):
        from product_radar.desktop_control import spend_summary
        self.observe(55, at="2026-10-14T19:00:00+00:00")
        with patch("product_radar.desktop_control.datetime") as clock:
            clock.now.return_value = spend_watch.timestamp("2026-10-15T10:00:00+00:00")
            result = spend_summary(self.root)
            self.assertEqual([line["text"] for line in result["lines"]][0], "Test allowance line (55% used).")
            self.assertEqual([row["kind"] for row in result["alerts"]], ["threshold"])
            self.assertEqual(set(result["alerts"][0]), {"alert_id", "kind", "message"})
            clock.now.return_value = spend_watch.timestamp("2026-10-16T10:00:00+00:00")
            self.assertEqual(spend_summary(self.root)["alerts"], [])  # older than a day: not notified again
        runtime_path(self.root, spend_watch.AREA, "state.json").write_text("{}")
        self.assertEqual(spend_summary(self.root)["lines"][0]["item"], "spend-watch")

    def test_two_accounts_are_labelled_without_identifiers(self):
        events, financial = ledger("0.20")
        other = "apify-ffffffffffffffffffffffff:2026-10-other"
        events.append({"kind": "budget", "at": NOW, "data": {"namespace": other}})
        financial[0][other] = {"allowance": "5"}
        refresh_ledger_allowances(self.root, FakeJournal(events, financial), as_of=NOW, settings=self.settings)
        labels = sorted(line["label"] for line in allowance_summary(self.root)["lines"])
        self.assertIn("Apify free allowance (account 1)", labels)
        self.assertIn("Apify free allowance (account 2)", labels)
        self.assertNotIn("apify-ffff", json.dumps(allowance_summary(self.root)))

    def test_money_in_rupees_flags_target_and_review_once(self):
        events, financial = ledger(*["0.42"] * 10)  # US$4.20 at ₹100 = ₹420 of ₹750
        refresh_ledger_allowances(self.root, FakeJournal(events, financial), as_of=NOW, settings=self.settings)
        money = load_state(self.root)["items"]["money-inr"]
        self.assertEqual((money["percent"], money["flags"], money["period"]), (56, [50], "2026-10"))
        self.assertEqual(money["text"], "Money this month: ₹420 of the ₹750 target.")
        with writing(self.root, self.settings) as (state, alerts):
            state["quickcommerce"]["detected_packs"] = [
                {"credits": 10000, "price_inr": "1800", "detected_at": NOW},
                {"credits": 900, "price_inr": None, "detected_at": NOW}]
        refresh_ledger_allowances(self.root, FakeJournal(events, financial), as_of=NOW, settings=self.settings)
        money = load_state(self.root)["items"]["money-inr"]
        self.assertEqual((money["percent"], money["flags"]), (296, [50, 75, 90, 100]))
        self.assertIn("1 credit purchase(s) have no recorded price", money["text"])
        refresh_ledger_allowances(self.root, FakeJournal(events, financial), as_of=NOW, settings=self.settings)
        reviews = [a for a in self.alerts() if a["kind"] == "review"]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["message"], "Money this month is ₹2,220, above ₹1,000: please review spending.")
        self.assertIsNone(money_route_wait(self.root, "quickcommerce"))  # money totals never pause a route
        refresh_ledger_allowances(self.root, FakeJournal(*ledger(namespace=PERIOD, at=NEXT_MONTH)), as_of=NEXT_MONTH,
                                  settings=self.settings)
        self.assertEqual(load_state(self.root)["items"]["money-inr"]["flags"], [])

    def test_meter_shows_allowances_from_saved_records_only(self):
        from product_radar.programme import programme_meter
        journal = Journal(runtime_path(self.root, "data/benchmarks/state/apify"))
        with journal.locked():
            journal.setup_budget(PERIOD, "5.00")
            journal.append("reserve-one", "reserved", {"attempt": "one", "namespace": PERIOD, "stage": "meta",
                           "cap": "0.20", "config_hash": "0" * 64, "generation": "g1"})
        memory = ResearchMemory(runtime_path(self.root, "data/research/captured"), journal)
        refresh_ledger_allowances(self.root, journal, as_of=NOW)
        with patch("urllib.request.urlopen", side_effect=AssertionError("provider call")), \
             patch("subprocess.Popen", side_effect=AssertionError("model or program call")):
            result = programme_meter(memory, root=self.root, as_of=NOW)
        self.assertNotIn("allowances", result["optional_input_failures"])
        texts = [line["text"] for line in result["allowances"]["lines"]]
        self.assertIn("Apify free allowance: US$0.20 of US$4.50 used (4%).", texts)
        import run_programme
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(run_programme.main(["--meter"], memory=memory, root=self.root), 0)
        self.assertEqual(json.loads(output.getvalue())["allowances"]["state"], "recorded")

    def test_meter_reports_an_unreadable_allowance_record_as_an_optional_failure(self):
        from product_radar.programme import programme_meter
        journal = Journal(runtime_path(self.root, "data/benchmarks/state/apify"))
        memory = ResearchMemory(runtime_path(self.root, "data/research/captured"), journal)
        path = runtime_path(self.root, spend_watch.AREA, "state.json")
        path.parent.mkdir(parents=True); path.write_text("{}")
        result = programme_meter(memory, root=self.root, as_of=NOW)
        self.assertIsNone(result["allowances"])
        self.assertEqual(result["optional_input_failures"]["allowances"], "SPEND_WATCH_STATE_INVALID")

    def test_programme_measurement_hook_refreshes_the_ledger_allowances(self):
        from product_radar.programme import _refresh_owner_measurements
        journal = FakeJournal(*ledger("0.20"))
        memory = type("Memory", (), {"journal": journal, "root": self.root / "data/research/captured"})()
        record, context = {}, {"at": NOW, "fixed_as_of": NOW, "clock": None, "root": self.root}
        with patch("product_radar.programme.programme_service_state", return_value={"enabled": True}), \
             patch("product_radar.operational_effort_cache.refresh_model_effort_checkpoint", return_value={}):
            _refresh_owner_measurements(memory, record, context, state={})
        self.assertEqual(record["owner_measurement_refresh"]["optional_input_failures"], {})
        self.assertIn("money-inr", load_state(self.root)["items"])
        record, context = {}, {"at": NOW, "fixed_as_of": NOW, "clock": None, "root": self.root}
        with patch("product_radar.programme.programme_service_state", return_value={"enabled": True}), \
             patch("product_radar.operational_effort_cache.refresh_model_effort_checkpoint", return_value={}), \
             patch("product_radar.spend_watch.load_settings", side_effect=SafetyError("SPEND_WATCH_SETTINGS_INVALID")):
            _refresh_owner_measurements(memory, record, context, state={})
        self.assertEqual(record["owner_measurement_refresh"]["optional_input_failures"],
                         {"spend_watch": "SPEND_WATCH_SETTINGS_INVALID"})

    def test_daily_backup_selects_state_alert_logs_and_settings(self):
        from product_radar.operational_backup_selection import select_essential_backup_files
        self.observe(80)
        settings = self.root / "config/research/spend_watch.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(spend_watch.SETTINGS.read_text())
        selected = select_essential_backup_files({table: {} for table in TABLES}, ({}, {}), root=self.root)
        rows = {row["relative_path"]: row for row in selected["files"]}
        self.assertEqual(rows["data/research/captured/spend_watch/state.json"]["kind"], "operating_control")
        self.assertEqual(rows["data/research/captured/spend_watch/alerts/2026-10-14.jsonl"]["kind"], "operating_control")
        self.assertEqual(rows["config/research/spend_watch.json"]["kind"], "public_setting")
        self.assertEqual(selected["unsafe"], [])
        self.assertNotIn("data/research/captured/spend_watch/runner.lock", rows)
        settings.write_text(json.dumps({"schema_version": 1}))
        selected = select_essential_backup_files({table: {} for table in TABLES}, ({}, {}), root=self.root)
        self.assertNotIn("config/research/spend_watch.json", {row["relative_path"] for row in selected["files"]})
        self.assertEqual(selected["unsafe"][0]["reason"], "SPEND_WATCH_SETTINGS_INVALID")

    def test_linked_alert_folder_is_refused_by_the_backup(self):
        from product_radar.operational_backup_selection import select_essential_backup_files
        elsewhere = self.root / "elsewhere"; elsewhere.mkdir()
        folder = runtime_path(self.root, spend_watch.AREA); folder.mkdir(parents=True)
        (folder / "alerts").symlink_to(elsewhere, target_is_directory=True)
        selected = select_essential_backup_files({table: {} for table in TABLES}, ({}, {}), root=self.root)
        self.assertIn({"origin": "spend_watch/alerts", "reason": "SYMLINK_PATH_REFUSED", "relative_path": None},
                      selected["unsafe"])


if __name__ == "__main__":
    unittest.main()
