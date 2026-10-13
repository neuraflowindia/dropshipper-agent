"""Offline commerce-connection checks; no real credentials or requests."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_quickcommerce_connection.py"
spec = importlib.util.spec_from_file_location("commerce_check", SCRIPT)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)
FAKE_KEY = "fictional-commerce-key-not-a-real-secret"
SUMMARY = {"total_available": 100, "total_used": 0, "active_packs": 1}


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
            self.assertEqual(check.parse_key(f"\ufeff# private\r\nQUICKCOMMERCE_API_KEY={key}\r\n"), FAKE_KEY)

    def test_invalid_settings_are_safe(self):
        for text in ("", "QUICKCOMMERCE_API_KEY=", "QUICKCOMMERCE_API_KEY=short",
                     f"QUICKCOMMERCE_API_KEY={FAKE_KEY}\nQUICKCOMMERCE_API_KEY={FAKE_KEY}",
                     f"QUICKCOMMERCE_API_KEY={FAKE_KEY} injected", "QUICKCOMMERCE_API_KEY=" + "x" * 513):
            with self.assertRaises(check.ConfigurationError) as caught:
                check.parse_key(text)
            self.assertNotIn(FAKE_KEY, str(caught.exception))

    def test_success_uses_only_credit_endpoint_and_header(self):
        body = {"summary": {**SUMMARY, "private": FAKE_KEY}, "active": [{"id": FAKE_KEY}], "inactive": []}
        client = response_client(json.dumps(body).encode())
        success, message, summary = check.check_connection(FAKE_KEY, client)
        self.assertTrue(success)
        self.assertEqual(summary, SUMMARY)
        self.assertNotIn(FAKE_KEY, message + json.dumps(summary))
        request = client.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "https://api.quickcommerceapi.com/v1/credits")
        self.assertEqual(request.get_header("X-api-key"), FAKE_KEY)
        self.assertIsNone(request.data)
        client.open.assert_called_once_with(request, timeout=20)
        client.open.return_value.__enter__.return_value.read.assert_called_once_with(check.MAX_RESPONSE_BYTES + 1)

    def test_zero_balance_is_valid_authentication(self):
        payload = {"summary": {"total_available": 0, "total_used": 100, "active_packs": 0}, "active": [], "inactive": []}
        self.assertIsNotNone(check.safe_summary(payload))

    def test_invalid_payloads_not_accepted(self):
        for body in (b"null", b"[]", b"{}", b"<html>login</html>", b"x" * 65537,
                     b'{"summary":{"total_available":true,"total_used":0,"active_packs":1},"active":[],"inactive":[]}',
                     b'{"summary":{"total_available":-1,"total_used":0,"active_packs":1},"active":[],"inactive":[]}'):
            success, _, summary = check.check_connection(FAKE_KEY, response_client(body))
            self.assertFalse(success)
            self.assertIsNone(summary)

    def test_http_errors_are_safe_and_do_not_retry(self):
        for code in (301, 302, 307, 401, 402, 403, 429, 500):
            client = Mock()
            client.open.side_effect = HTTPError(check.ENDPOINT, code, FAKE_KEY, {}, io.BytesIO(FAKE_KEY.encode()))
            success, message, summary = check.check_connection(FAKE_KEY, client)
            self.assertFalse(success)
            self.assertNotIn(FAKE_KEY, message)
            self.assertIsNone(summary)
            self.assertEqual(client.open.call_count, 1)

    def test_network_failure_is_not_invalid_key(self):
        client = Mock()
        client.open.side_effect = URLError(FAKE_KEY)
        success, message, summary = check.check_connection(FAKE_KEY, client)
        self.assertFalse(success)
        self.assertIn("unverified", message)
        self.assertNotIn(FAKE_KEY, message)
        self.assertIsNone(summary)

    def test_redirect_is_blocked(self):
        self.assertIsNone(check.NoRedirects().redirect_request(None, None, 302, "", {}, "https://example.com"))

    def test_cli_needs_explicit_live_and_hides_exceptions(self):
        with patch.object(check, "load_key") as load, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check.main([]), 2)
            load.assert_not_called()
        output = io.StringIO()
        with patch.object(check, "load_key", side_effect=RuntimeError(FAKE_KEY)), contextlib.redirect_stdout(output):
            self.assertEqual(check.main(["--live"]), 2)
        self.assertNotIn(FAKE_KEY, output.getvalue())


if __name__ == "__main__":
    unittest.main()
