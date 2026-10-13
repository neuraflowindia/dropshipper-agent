"""Offline tests using fictional keys only; never access the private file."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_apify_connection.py"
spec = importlib.util.spec_from_file_location("apify_connection", SCRIPT)
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)
FAKE_KEY = "fictional-test-key-not-a-real-secret"


def fake_response(body, status=200):
    response = Mock(status=status)
    response.read.return_value = body
    context = Mock()
    context.__enter__ = Mock(return_value=response)
    context.__exit__ = Mock(return_value=False)
    return context


class ApifyConnectionTests(unittest.TestCase):
    def test_windows_file_and_optional_quotes(self):
        for value in (FAKE_KEY, f'"{FAKE_KEY}"', f"'{FAKE_KEY}'"):
            with self.subTest(value=value):
                self.assertEqual(check.parse_token(f"\ufeff# private\r\nAPIFY_TOKEN={value}\r\n"), FAKE_KEY)

    def test_invalid_or_duplicate_settings(self):
        for contents in ("", "APIFY_TOKEN=", "APIFY_TOKEN=short", "APIFY_TOKEN",
                         f"APIFY_TOKEN={FAKE_KEY}\nAPIFY_TOKEN={FAKE_KEY}",
                         f"APIFY_TOKEN={FAKE_KEY} injected", "APIFY_TOKEN=" + "a" * 513):
            with self.subTest(case=len(contents)):
                with self.assertRaises(check.ConfigurationError) as caught:
                    check.parse_token(contents)
                self.assertNotIn(FAKE_KEY, str(caught.exception))

    def test_success_is_bounded_get_with_header_auth_only(self):
        client = Mock()
        client.open.return_value = fake_response(json.dumps({"data": {
            "items": [], "count": 0, "total": 0, "offset": 0}}).encode())
        success, message = check.check_connection(FAKE_KEY, client)
        self.assertTrue(success)
        self.assertNotIn(FAKE_KEY, message)
        request = client.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, check.ENDPOINT)
        self.assertIsNone(request.data)
        self.assertNotIn(FAKE_KEY, request.full_url)
        self.assertEqual(request.get_header("Authorization"), f"Bearer {FAKE_KEY}")
        client.open.assert_called_once_with(request, timeout=20)
        client.open.return_value.__enter__.return_value.read.assert_called_once_with(check.MAX_RESPONSE_BYTES + 1)

    def test_http_errors_never_print_body_or_token(self):
        for code, expected in ((401, "401"), (403, "403"), (429, "429"),
                               (302, "Redirect"), (500, "HTTP error")):
            with self.subTest(code=code):
                client = Mock()
                client.open.side_effect = HTTPError(check.ENDPOINT, code, FAKE_KEY, {}, io.BytesIO(FAKE_KEY.encode()))
                success, message = check.check_connection(FAKE_KEY, client)
                self.assertFalse(success)
                self.assertIn(expected, message)
                self.assertNotIn(FAKE_KEY, message)
                self.assertEqual(client.open.call_count, 1)

    def test_network_errors_are_safe(self):
        for error in (URLError(FAKE_KEY), TimeoutError(FAKE_KEY), OSError(FAKE_KEY)):
            client = Mock()
            client.open.side_effect = error
            success, message = check.check_connection(FAKE_KEY, client)
            self.assertFalse(success)
            self.assertNotIn(FAKE_KEY, message)
            self.assertIn("unverified", message)

    def test_bad_response_not_treated_as_authenticated_success(self):
        for body in (b"<html>login</html>", b"{}", b"[]", b"null", b'{}' * 40000,
                     b'{"data":{"items":[],"count":true,"total":0,"offset":0}}',
                     b'{"data":{"items":[],"count":1,"total":1,"offset":0}}'):
            client = Mock()
            client.open.return_value = fake_response(body)
            self.assertFalse(check.check_connection(FAKE_KEY, client)[0])

    def test_redirects_never_follow(self):
        self.assertIsNone(check.NoRedirects().redirect_request(None, None, 302, "", {}, "https://example.com"))

    def test_cli_requires_explicit_live_flag(self):
        with patch.object(check, "load_token") as load, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check.main([]), 2)
            load.assert_not_called()

    def test_cli_suppresses_unexpected_secret_bearing_error(self):
        output = io.StringIO()
        with patch.object(check, "load_token", return_value=FAKE_KEY), \
             patch.object(check, "check_connection", side_effect=RuntimeError(FAKE_KEY)), \
             contextlib.redirect_stdout(output):
            self.assertEqual(check.main(["--live"]), 2)
        self.assertNotIn(FAKE_KEY, output.getvalue())


if __name__ == "__main__":
    unittest.main()
