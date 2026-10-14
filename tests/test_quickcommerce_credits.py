"""The credit gate before paid quick-commerce batches (Plan R10 W5 with W12).

Synthetic balance readings and replies only; no real key or paid search.
"""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.models import SafetyError
from product_radar.quickcommerce import map_credits
from product_radar.quickcommerce_credits import CreditGate
from product_radar.spend_watch import load_state
from product_radar.spend_watch_quickcommerce import credit_wait, record_credits_exhausted
from test_quickcommerce import credits_payload

NOW = "2026-10-09T10:00:00+00:00"
NEXT_PASS = "2026-10-09T10:01:00+00:00"


class Client:
    def __init__(self, *available):
        self.available, self.calls = list(available), 0

    def credits(self):
        self.calls += 1
        value = self.available.pop(0) if len(self.available) > 1 else self.available[0]
        if isinstance(value, Exception):
            raise value
        return map_credits(credits_payload(value))


class CreditGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="quickcommerce-credits-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_one_free_check_per_pass_and_a_new_check_for_each_new_pass(self):
        client = Client(44)
        gate = CreditGate(self.root, client)
        self.assertIsNone(gate.admit(NOW)); self.assertIsNone(gate.admit(NOW))
        self.assertEqual(client.calls, 1)
        self.assertIsNone(gate.admit(NEXT_PASS))
        self.assertEqual(client.calls, 2)

    def test_a_refusal_is_kept_for_the_pass_with_its_recheck_time(self):
        client = Client(0)
        gate = CreditGate(self.root, client)
        wait = gate.admit(NOW)
        self.assertEqual(wait, {"reason": "QUICKCOMMERCE_CREDITS_EXHAUSTED", "next_retry_at": "2026-10-09T11:00:00+00:00"})
        self.assertEqual(gate.admit(NOW), wait); self.assertEqual(client.calls, 1)
        with self.assertRaises(SafetyError):
            gate.admit("not a time")

    def test_a_wait_recorded_mid_pass_sends_the_gate_back_to_the_free_check(self):
        client = Client(5, 0)
        gate = CreditGate(self.root, client)
        self.assertIsNone(gate.admit(NOW))
        record_credits_exhausted(self.root, as_of=NOW)
        self.assertEqual(gate.admit(NOW)["reason"], "QUICKCOMMERCE_CREDITS_EXHAUSTED")
        self.assertEqual(client.calls, 2)

    def test_an_unknown_balance_fails_closed_with_a_short_recheck(self):
        gate = CreditGate(self.root, Client(SafetyError("QUICKCOMMERCE_RATE_LIMITED")))
        self.assertEqual(gate.admit(NOW), {"reason": "QUICKCOMMERCE_RATE_LIMITED",
                                           "next_retry_at": "2026-10-09T10:10:00+00:00"})

    def test_replies_are_counted_and_a_no_credits_refusal_sets_the_wait(self):
        gate = CreditGate(self.root, Client(44))
        gate.record({"http_status": 200, "credits_remaining": 12, "observed_at": NOW})
        self.assertEqual(load_state(self.root)["quickcommerce"]["last_remaining"]["credits_remaining"], 12)
        gate.record({"http_status": 200, "credits_remaining": None, "observed_at": NOW})  # nothing to count
        self.assertIsNone(credit_wait(self.root))
        gate.record({"http_status": 402, "observed_at": NOW})
        self.assertEqual(credit_wait(self.root)["source"], "provider_refusal")

    def test_a_spend_record_problem_is_logged_and_never_raised(self):
        gate = CreditGate(self.root, Client(44))
        with patch("product_radar.quickcommerce_credits.record_response_remaining",
                   side_effect=SafetyError("RUNNER_ALREADY_ACTIVE")), \
             patch("product_radar.operations.record_error") as logged:
            gate.record({"http_status": 200, "credits_remaining": 3, "observed_at": NOW})
        self.assertEqual(logged.call_args.args[0], "spend_watch_refresh_failed")
        self.assertEqual(str(logged.call_args.args[1]), "RUNNER_ALREADY_ACTIVE")

if __name__ == "__main__":
    unittest.main()
