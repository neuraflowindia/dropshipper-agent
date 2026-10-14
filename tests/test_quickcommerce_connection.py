"""Offline commerce-connection checks; no real credentials or requests.

The key file reader and free balance check live in the library
(``product_radar.quickcommerce``); the script only displays validated totals.
"""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.models import SafetyError
from product_radar.quickcommerce import (CREDITS_ENDPOINT, MAX_CREDITS_BYTES, QuickCommerceClient, load_key,
                                         map_credits, parse_key)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_quickcommerce_connection.py"
spec = importlib.util.spec_from_file_location("commerce_check", SCRIPT)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)
FAKE_KEY = "fictional-commerce-key-not-a-real-secret"
SUMMARY = {"total_available": 100, "total_used": 0, "active_packs": 1}
PACK = {"id": "provider-pack-id", "pack_name": "Free trial", "credits_total": 100, "credits_remaining": 100,
        "credits_used": 0, "amount_paid": 0.0, "is_trial": True, "status": "active",
        "purchased_at": "2026-10-03T08:45:38.026844+00:00", "expires_at": "2026-11-02T08:45:38.023824+00:00"}


def response_client(body, status=200):
    response = Mock(status=status)
    response.read.return_value = body
    context = Mock()
    context.__enter__ = Mock(return_value=response)
    context.__exit__ = Mock(return_value=False)
    client = Mock()
    client.open.return_value = context
    return client


class QuickCommerceConnectionTests(unittest.TestCase):
    def test_windows_key_file_and_quotes(self):
        for key in (FAKE_KEY, f'"{FAKE_KEY}"', f"'{FAKE_KEY}'"):
            self.assertEqual(parse_key(f"﻿# private\r\nQUICKCOMMERCE_API_KEY={key}\r\n"), FAKE_KEY)

    def test_invalid_settings_are_safe(self):
        for text in ("", "QUICKCOMMERCE_API_KEY", "QUICKCOMMERCE_API_KEY=", "QUICKCOMMERCE_API_KEY=short",
                     f"QUICKCOMMERCE_API_KEY={FAKE_KEY}\nQUICKCOMMERCE_API_KEY={FAKE_KEY}",
                     f"QUICKCOMMERCE_API_KEY={FAKE_KEY} injected", "QUICKCOMMERCE_API_KEY=" + "x" * 513):
            with self.assertRaisesRegex(SafetyError, "QUICKCOMMERCE_KEY_FILE_INVALID") as caught:
                parse_key(text)
            self.assertNotIn(FAKE_KEY, str(caught.exception))

    def test_key_file_reading_is_bounded_and_safe(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "quickcommerce.env"
            path.write_text(f"QUICKCOMMERCE_API_KEY={FAKE_KEY}\n")
            self.assertEqual(load_key(path), FAKE_KEY)
            path.write_bytes(b"#" * 8193)
            with self.assertRaisesRegex(SafetyError, "QUICKCOMMERCE_KEY_FILE_INVALID"):
                load_key(path)
            path.write_bytes(b"\xff\xfe\xfa")
            with self.assertRaisesRegex(SafetyError, "QUICKCOMMERCE_KEY_FILE_UNREADABLE"):
                load_key(path)
            with self.assertRaisesRegex(SafetyError, "QUICKCOMMERCE_KEY_FILE_UNREADABLE"):
                load_key(Path(folder) / "missing.env")

    def test_success_uses_only_credit_endpoint_and_header(self):
        body = {"summary": {**SUMMARY, "private": FAKE_KEY}, "active": [dict(PACK, note=FAKE_KEY)], "inactive": []}
        client = response_client(json.dumps(body).encode())
        reading = QuickCommerceClient(FAKE_KEY, opener=client).credits()
        self.assertEqual({k: reading[k] for k in SUMMARY}, SUMMARY)
        self.assertEqual(reading["packs"], [{"credits_total": 100, "credits_remaining": 100, "credits_used": 0,
            "is_trial": True, "purchased_at": "2026-10-03T08:45:38.026844+00:00",
            "expires_at": "2026-11-02T08:45:38.023824+00:00"}])
        self.assertNotIn(FAKE_KEY, json.dumps(reading))
        self.assertNotIn("provider-pack-id", json.dumps(reading))
        request = client.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, CREDITS_ENDPOINT)
        self.assertEqual(request.get_header("X-api-key"), FAKE_KEY)
        self.assertIsNone(request.data)
        client.open.assert_called_once_with(request, timeout=20)
        client.open.return_value.__enter__.return_value.read.assert_called_once_with(MAX_CREDITS_BYTES + 1)

    def test_zero_balance_is_valid_authentication(self):
        payload = {"summary": {"total_available": 0, "total_used": 100, "active_packs": 0}, "active": [], "inactive": []}
        self.assertEqual(map_credits(payload)["total_available"], 0)
        payload = {"summary": SUMMARY, "active": [dict(PACK, purchased_at=None, expires_at=None)], "inactive": [PACK]}
        mapped = map_credits(payload)
        self.assertEqual([(p["purchased_at"], p["expires_at"]) for p in mapped["packs"]], [(None, None)])
        self.assertEqual([p["credits_total"] for p in mapped["inactive_packs"]], [100])  # expired packs are still purchases

    def test_invalid_payloads_not_accepted(self):
        bodies = [b"null", b"[]", b"{}", b"<html>login</html>", b"x" * (MAX_CREDITS_BYTES + 1),
                  b'{"summary":{"total_available":true,"total_used":0,"active_packs":1},"active":[],"inactive":[]}',
                  b'{"summary":{"total_available":-1,"total_used":0,"active_packs":1},"active":[],"inactive":[]}']
        for pack in (dict(PACK, credits_used=-1), dict(PACK, is_trial="yes"), dict(PACK, expires_at="soon"),
                     dict(PACK, purchased_at=7), dict(PACK, expires_at="2026-11-02"), "pack"):
            bodies.append(json.dumps({"summary": SUMMARY, "active": [pack], "inactive": []}).encode())
        bodies.append(json.dumps({"summary": SUMMARY, "active": [PACK] * 101, "inactive": []}).encode())
        bodies.append(json.dumps({"summary": SUMMARY, "active": [], "inactive": [PACK] * 501}).encode())
        bodies.append(json.dumps({"summary": SUMMARY, "active": [], "inactive": [dict(PACK, credits_total="100")]}).encode())
        for body in bodies:
            with self.subTest(body=body[:80]), self.assertRaisesRegex(SafetyError, "QUICKCOMMERCE_CREDITS_RESPONSE_INVALID"):
                QuickCommerceClient(FAKE_KEY, opener=response_client(body)).credits()
        with self.assertRaisesRegex(SafetyError, "QUICKCOMMERCE_CREDITS_HTTP_UNEXPECTED"):
            QuickCommerceClient(FAKE_KEY, opener=response_client(b"{}", status=204)).credits()

    def test_http_errors_are_safe_and_do_not_retry(self):
        codes = {401: "QUICKCOMMERCE_AUTH_REJECTED", 402: "QUICKCOMMERCE_CREDITS_CHECK_REFUSED",
                 403: "QUICKCOMMERCE_ACCESS_DENIED", 429: "QUICKCOMMERCE_RATE_LIMITED",
                 301: "QUICKCOMMERCE_PROVIDER_HTTP_FAILED", 500: "QUICKCOMMERCE_PROVIDER_HTTP_FAILED"}
        for code, reason in codes.items():
            client = Mock()
            client.open.side_effect = HTTPError(CREDITS_ENDPOINT, code, FAKE_KEY, {}, io.BytesIO(FAKE_KEY.encode()))
            with self.subTest(code=code), self.assertRaises(SafetyError) as caught:
                QuickCommerceClient(FAKE_KEY, opener=client).credits()
            self.assertEqual(str(caught.exception), reason)
            self.assertEqual(client.open.call_count, 1)
            success, message, summary = check.check_connection(QuickCommerceClient(FAKE_KEY, opener=client))
            self.assertFalse(success)
            self.assertIsNone(summary)
            self.assertNotIn(FAKE_KEY, message)

    def test_network_failure_is_not_invalid_key(self):
        client = Mock()
        client.open.side_effect = URLError(FAKE_KEY)
        success, message, summary = check.check_connection(QuickCommerceClient(FAKE_KEY, opener=client))
        self.assertFalse(success)
        self.assertIn("unverified", message)
        self.assertNotIn(FAKE_KEY, message)
        self.assertIsNone(summary)

    def test_script_shows_only_validated_totals(self):
        body = {"summary": SUMMARY, "active": [PACK], "inactive": []}
        success, message, summary = check.check_connection(
            QuickCommerceClient(FAKE_KEY, opener=response_client(json.dumps(body).encode())))
        self.assertTrue(success)
        self.assertEqual(summary, SUMMARY)
        self.assertIn("No product search was made", message)

    def test_cli_needs_explicit_live_and_hides_exceptions(self):
        with patch.object(check, "load_key") as load, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check.main([]), 2)
            load.assert_not_called()
        for error in (RuntimeError(FAKE_KEY), SafetyError("QUICKCOMMERCE_KEY_FILE_UNREADABLE")):
            output = io.StringIO()
            with patch.object(check, "load_key", side_effect=error), contextlib.redirect_stdout(output):
                self.assertEqual(check.main(["--live"]), 2)
            self.assertNotIn(FAKE_KEY, output.getvalue())

    def test_cli_live_check_prints_totals_or_failure(self):
        body = json.dumps({"summary": SUMMARY, "active": [PACK], "inactive": []}).encode()
        for opener, code, marker in ((response_client(body), 0, "PASS"), (response_client(b"{}"), 1, "CHECK FAILED")):
            output = io.StringIO()
            with patch.object(check, "load_key", return_value=FAKE_KEY), \
                 patch.object(check, "QuickCommerceClient", side_effect=lambda key, opener=opener: QuickCommerceClient(key, opener=opener)), \
                 contextlib.redirect_stdout(output):
                self.assertEqual(check.main(["--live"]), code)
            self.assertIn(marker, output.getvalue())
            self.assertNotIn(FAKE_KEY, output.getvalue())


if __name__ == "__main__":
    unittest.main()
