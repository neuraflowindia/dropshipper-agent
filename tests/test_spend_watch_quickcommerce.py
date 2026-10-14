"""Quick-commerce credit allowance: packs, expiry, exhausted wait, gate and clearing.

Synthetic balance readings only; no provider search, purchase or real key.
"""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.models import SafetyError
from product_radar.spend_watch import allowance_summary, load_settings, load_state, money_route_wait
from product_radar.spend_watch_quickcommerce import (check_credits_before_batch, credit_wait, quickcommerce_gate,
                                                     record_credit_reading, record_credits_exhausted,
                                                     record_response_remaining)

TRIAL = {"credits_total": 100, "credits_remaining": 44, "credits_used": 56, "is_trial": True,
         "purchased_at": "2026-10-03T08:45:38+00:00", "expires_at": "2026-11-02T08:45:38+00:00"}
STARTER = {"credits_total": 2500, "credits_remaining": 2500, "credits_used": 0, "is_trial": False,
           "purchased_at": "2026-10-16T09:00:00+00:00", "expires_at": "2026-11-15T09:00:00+00:00"}
NOW = "2026-10-14T10:00:00+00:00"
LATER = "2026-10-16T10:00:00+00:00"


def reading(available, used, *packs, inactive=()):
    return {"total_available": available, "total_used": used, "active_packs": len(packs), "packs": list(packs),
            "inactive_packs": list(inactive)}


class Client:
    def __init__(self, value):
        self.value, self.calls = value, 0

    def credits(self):
        self.calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class QuickCommerceAllowanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="spend-watch-commerce-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = load_settings()

    def record(self, value, at=NOW):
        return record_credit_reading(self.root, value, as_of=at, settings=self.settings)

    def item(self):
        return load_state(self.root)["items"]["quickcommerce"]

    def alerts(self, kind=None):
        rows = allowance_summary(self.root, settings=self.settings, alert_limit=200)["alerts"]
        return [row for row in rows if kind is None or row["kind"] == kind]

    def detected(self):
        return load_state(self.root)["quickcommerce"].get("detected_packs", [])

    def test_used_over_held_with_flags_and_expiry(self):
        result = self.record(reading(44, 56, TRIAL))
        self.assertEqual(result, {"available": 44, "wait": None})
        item = self.item()
        self.assertEqual((item["percent"], item["flags"], item["allowance"]), (56, [50], "100"))
        self.assertEqual(item["text"], "Quick-commerce credits: 56 of 100 used, 44 left in 1 active pack(s); earliest expiry 2026-11-02.")
        self.record(reading(9, 91, dict(TRIAL, credits_remaining=9, credits_used=91)))
        self.assertEqual(self.item()["flags"], [50, 75, 90])
        self.assertEqual([a["threshold"] for a in self.alerts("threshold")], [50, 75, 90])
        self.assertEqual(self.detected(), [])  # the free trial pack is not a purchase
        self.assertIsNone(credit_wait(self.root))

    def test_zero_balance_waits_and_a_bought_pack_clears_the_wait(self):
        self.record(reading(0, 100, dict(TRIAL, credits_remaining=0, credits_used=100)))
        wait = credit_wait(self.root)
        self.assertEqual((wait["reason"], wait["source"]), ("QUICKCOMMERCE_CREDITS_EXHAUSTED", "balance_check"))
        self.assertEqual(money_route_wait(self.root, "quickcommerce"), wait)
        self.assertEqual(self.item()["flags"], [50, 75, 90, 100])
        result = self.record(reading(2500, 100, STARTER, inactive=[dict(TRIAL, credits_remaining=0, credits_used=100)]), at=LATER)
        self.assertIsNone(result["wait"])
        self.assertIsNone(credit_wait(self.root))
        self.assertEqual([a["kind"] for a in self.alerts() if a["kind"] in {"paused", "resumed"}], ["paused", "resumed"])
        self.assertEqual((self.item()["percent"], self.item()["flags"]), (0, []))  # a new pack is a new period
        pack = self.detected()[0]
        self.assertEqual({k: pack[k] for k in ("credits", "price_inr", "purchased_at", "detected_at", "expires_at")},
                         {"credits": 2500, "price_inr": "500", "purchased_at": STARTER["purchased_at"], "detected_at": LATER,
                          "expires_at": STARTER["expires_at"]})
        self.assertIn("A ₹500 quick-commerce pack (2,500 credits) was bought. It expires on 2026-11-15.",
                      [a["message"] for a in self.alerts("notice")])
        money = load_state(self.root)["items"]["money-inr"]
        self.assertEqual((money["used"], money["flags"]), ("500", [50]))

    def test_packs_come_from_the_provider_list_including_expired_ones_once(self):
        expired = dict(STARTER, credits_total=10000, credits_remaining=0, credits_used=10000,
                       purchased_at="2026-09-10T00:00:00+00:00", expires_at="2026-10-10T00:00:00+00:00")
        odd = dict(STARTER, credits_total=777, purchased_at="2026-10-15T00:00:00+00:00", expires_at=None)
        self.record(reading(3277, 10000, STARTER, odd, inactive=[expired]))
        self.record(reading(3200, 10077, dict(STARTER, credits_remaining=2423, credits_used=77), odd, inactive=[expired]), at=LATER)
        packs = {pack["credits"]: pack for pack in self.detected()}
        self.assertEqual(sorted(packs), [777, 2500, 10000])
        self.assertEqual((packs[10000]["price_inr"], packs[777]["price_inr"]), ("1800", None))
        notices = [a["message"] for a in self.alerts("notice")]
        self.assertEqual(len(notices), 3)
        self.assertTrue(any("matches no published price" in message for message in notices))
        money = load_state(self.root)["items"]["money-inr"]  # by purchase month: September is not October
        self.assertEqual(money["used"], "500")
        self.assertIn("1 credit purchase(s) have no recorded price", money["text"])

    def test_expiry_warning_once_per_pack(self):
        soon = dict(TRIAL, expires_at="2026-10-16T08:00:00+00:00")
        self.record(reading(44, 56, soon))
        self.record(reading(40, 60, dict(soon, credits_remaining=40, credits_used=60)))
        expiry = self.alerts("expiry")
        self.assertEqual(len(expiry), 1)
        self.assertEqual(expiry[0]["message"], "44 quick-commerce credits expire on 2026-10-16.")
        self.record(reading(0, 100, dict(soon, credits_remaining=0, credits_used=100, expires_at="2026-10-15T00:00:00+00:00")))
        self.assertEqual(len(self.alerts("expiry")), 1)  # nothing left to lose

    def test_expired_packs_are_not_held_credits(self):
        expired = dict(TRIAL, expires_at="2026-10-13T00:00:00+00:00")
        self.record(reading(0, 56, expired))
        self.assertEqual(self.item()["text"], "Quick-commerce credits: 0 of 0 used, 0 left.")
        self.assertEqual(self.item()["percent"], 100)

    def test_search_responses_update_between_checks_and_zero_waits(self):
        record_response_remaining(self.root, 10, as_of=NOW, settings=self.settings)
        self.assertNotIn("quickcommerce", load_state(self.root)["items"])
        self.record(reading(44, 56, TRIAL))
        self.assertIsNone(record_response_remaining(self.root, 20, as_of=NOW, settings=self.settings))
        self.assertEqual((self.item()["percent"], self.item()["flags"]), (80, [50, 75]))
        wait = record_response_remaining(self.root, 0, as_of=NOW, settings=self.settings)
        self.assertEqual((wait["reason"], wait["source"]), ("QUICKCOMMERCE_CREDITS_EXHAUSTED", "search_response"))
        self.assertEqual(self.item()["percent"], 100)
        for bad in (-1, True, "3"):
            with self.subTest(bad=bad), self.assertRaisesRegex(SafetyError, "QUICKCOMMERCE_CREDITS_RESPONSE_INVALID"):
                record_response_remaining(self.root, bad, as_of=NOW, settings=self.settings)

    def test_provider_refusal_sets_the_route_wide_wait(self):
        wait = record_credits_exhausted(self.root, as_of=NOW, settings=self.settings)
        self.assertEqual((wait["reason"], wait["source"], wait["since"]), ("QUICKCOMMERCE_CREDITS_EXHAUSTED", "provider_refusal", NOW))
        again = record_credits_exhausted(self.root, as_of=LATER, settings=self.settings)
        self.assertEqual(again["since"], NOW)
        self.assertEqual(len(self.alerts("paused")), 1)
        self.assertTrue(allowance_summary(self.root, settings=self.settings)["waits"])

    def test_gate_always_checks_first_so_a_purchase_clears_the_wait(self):
        record_credits_exhausted(self.root, as_of=NOW, settings=self.settings)
        empty = Client(reading(0, 100, inactive=[dict(TRIAL, credits_remaining=0, credits_used=100)]))
        self.assertEqual(quickcommerce_gate(self.root, empty, as_of=NOW, settings=self.settings),
                         {"allowed": False, "reason": "QUICKCOMMERCE_CREDITS_EXHAUSTED", "available": 0})
        bought = Client(reading(2500, 100, STARTER))
        self.assertEqual(quickcommerce_gate(self.root, bought, as_of=LATER, settings=self.settings),
                         {"allowed": True, "reason": None, "available": 2500})
        self.assertEqual((empty.calls, bought.calls), (1, 1))
        failing = Client(SafetyError("QUICKCOMMERCE_NETWORK_FAILED"))
        self.assertEqual(quickcommerce_gate(self.root, failing, as_of=LATER, settings=self.settings),
                         {"allowed": False, "reason": "QUICKCOMMERCE_NETWORK_FAILED", "available": None})

    def test_balance_check_before_batch_records_and_failures_are_kept(self):
        client = Client(reading(44, 56, TRIAL))
        self.assertEqual(check_credits_before_batch(self.root, client, as_of=NOW, settings=self.settings)["available"], 44)
        self.assertEqual(client.calls, 1)
        failing = Client(SafetyError("QUICKCOMMERCE_RATE_LIMITED"))
        with self.assertRaisesRegex(SafetyError, "QUICKCOMMERCE_RATE_LIMITED"):
            check_credits_before_batch(self.root, failing, as_of=LATER, settings=self.settings)
        self.assertEqual(load_state(self.root)["quickcommerce"]["last_check_failure"],
                         {"at": LATER, "reason": "QUICKCOMMERCE_RATE_LIMITED"})
        check_credits_before_batch(self.root, client, as_of=LATER, settings=self.settings)
        self.assertNotIn("last_check_failure", load_state(self.root)["quickcommerce"])
        for bad in (None, {"total_available": "44", "packs": [], "inactive_packs": []}, {"total_available": 4, "packs": []}):
            with self.subTest(bad=bad), self.assertRaisesRegex(SafetyError, "QUICKCOMMERCE_CREDITS_RESPONSE_INVALID"):
                self.record(bad)

    def test_saved_records_hold_no_pack_ids_or_keys(self):
        self.record(reading(44, 56, TRIAL, STARTER))
        text = json.dumps(load_state(self.root))
        for private in ("X-API-Key", "provider-pack-id", "api_key"):
            self.assertNotIn(private, text)


if __name__ == "__main__":
    unittest.main()
