"""Fictional service credentials and local fake transport; no cloud calls."""

from pathlib import Path
import hashlib
import os
import sys
import tempfile
import traceback
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.draft_automation import load_draft_automation_storage, private_staging_callback
from product_radar.models import SafetyError, canonical
from product_radar.portal_storage import DraftAutomationStorage, MAX_INDEX, _perform_raw, _validate_spec
from product_radar.storage import atomic_json
from test_portal_storage import FakeTransport, response, snapshot, INDEX, KEY, ORIGIN, CLIENT

SERVICE_ID = "a" * 32 + ".access"
SERVICE_SECRET = "manufactured-not-an-actual-secret" + "x" * 32
BASE = "/_automation/drafts"


class DraftAutomationTests(unittest.TestCase):
    def storage(self, responses=()):
        wire = FakeTransport(responses)
        return DraftAutomationStorage(ORIGIN, CLIENT, SERVICE_ID, SERVICE_SECRET, transport=wire), wire

    def spec(self, path=BASE + "/index", method="GET", raw=b""):
        headers = {"CF-Access-Client-Id": SERVICE_ID, "CF-Access-Client-Secret": SERVICE_SECRET,
                   "X-NeuraFlow-Release-Action": "v1", "Accept-Encoding": "identity"}
        if raw:
            headers["Content-Type"] = "application/pdf"
            headers["X-Content-SHA256"] = hashlib.sha256(raw).hexdigest()
        return {"method": method, "url": ORIGIN + path, "headers": headers, "max_response_bytes": MAX_INDEX}

    def test_index_read_uses_only_machine_headers_and_fixed_path(self):
        store, wire = self.storage([response(snapshot())])
        self.assertEqual(store.read_index(INDEX), snapshot())
        method, url, args = wire.calls[0]
        self.assertEqual((method, url), ("GET", ORIGIN + BASE + "/index"))
        self.assertNotIn("cf-access-token", args["headers"])
        self.assertEqual(args["headers"]["CF-Access-Client-Secret"], SERVICE_SECRET)
        _validate_spec(self.spec(), b"")

    def test_exact_immutable_pdf_put_and_readback_use_one_client(self):
        raw = b"%PDF-1.7\nFictional draft"; sha = hashlib.sha256(raw).hexdigest()
        headers = {"content-type": "application/pdf", "content-length": str(len(raw)), "x-content-sha256": sha}
        store, wire = self.storage([response({"key": KEY, "size": len(raw), "sha256": sha}), response(raw, headers=headers)])
        store.put_immutable(KEY, raw, sha256=sha, media_type="application/pdf")
        self.assertEqual(store.read_object(KEY), raw)
        self.assertEqual([x[0] for x in wire.calls], ["PUT", "GET"])
        self.assertTrue(all(x[1] == ORIGIN + BASE + "/objects/issue-one/1/report-en.pdf" for x in wire.calls))

    def test_machine_cannot_write_index_or_use_owner_pages(self):
        store, wire = self.storage()
        for call in (lambda: store.compare_and_swap_index(INDEX, None, {"kind": "activate"}),
                     store.read_private_draft_list,
                     lambda: store.read_private_draft_download(KEY, sha256="f" * 64, size=10),
                     lambda: store._call("POST", "/admin/index", raw=b"{}")):
            with self.assertRaisesRegex(SafetyError, "PORTAL_DRAFT_ONLY_ROUTE_REQUIRED"):
                call()
        self.assertEqual(wire.calls, [])

    def test_child_transport_rejects_machine_headers_on_owner_routes_and_forbidden_paths(self):
        for path, method in [("/admin/index", "GET"), (BASE + "/index", "POST"), (BASE + "/index", "DELETE"),
                             (BASE + "/drafts", "GET"), (BASE + "/download/issue-one/1/report-en.pdf", "GET"),
                             (BASE + "/objects/issue-one/1/secret.json", "GET"), (BASE + "/index?activate=yes", "GET")]:
            with self.subTest(path=path, method=method), self.assertRaises(SafetyError):
                _validate_spec(self.spec(path, method), b"")

    def test_bad_credentials_and_foreign_keys_never_reach_transport(self):
        for identity, secret in [(None, SERVICE_SECRET), ("someone@example.com", SERVICE_SECRET),
                                 (SERVICE_ID, "short"), (SERVICE_ID, SERVICE_SECRET + "\nX: injected")]:
            with self.assertRaises(SafetyError):
                DraftAutomationStorage(ORIGIN, CLIENT, identity, secret)
        store, wire = self.storage()
        for key in [KEY.replace(CLIENT, "another-client"), KEY + "?path=other", KEY.replace("report-en.pdf", "secret.json")]:
            with self.assertRaises(SafetyError):
                store.read_object(key)
        self.assertEqual(wire.calls, [])

    def test_redirect_denial_secret_reflection_and_transport_errors_do_not_leak(self):
        for result in [response(b"login", 302), response(b"denied", 403), response(SERVICE_ID.encode()),
                       response(SERVICE_SECRET.encode()), TimeoutError(SERVICE_SECRET)]:
            store, wire = self.storage([result])
            with self.assertRaises(SafetyError) as caught:
                store.read_index(INDEX)
            text = "".join(traceback.format_exception(caught.exception))
            self.assertNotIn(SERVICE_ID, text); self.assertNotIn(SERVICE_SECRET, text)
            self.assertEqual(len(wire.calls), 1)
        store, _ = self.storage()
        self.assertNotIn(SERVICE_SECRET, repr(store)); self.assertNotIn(SERVICE_ID, repr(store))

    def test_child_uses_verified_https_without_redirect_following(self):
        class Connection:
            def __init__(self, host, timeout): self.status = 302
            def request(self, method, path, **kwargs): self.path = path
            def getresponse(self): return self
            def getheaders(self): return [("Location", "https://other.example/private")]
            def read(self, limit): raise AssertionError("must not read redirected login")
            def close(self): pass
        result = _perform_raw(self.spec(), b"", connection_factory=Connection)
        self.assertEqual(result.status, 302); self.assertEqual(result.body, b"")

    def test_authenticated_missing_fixed_object_is_distinct_from_read_failure(self):
        store, wire = self.storage([response(b"ADMIN_OBJECT_NOT_FOUND", 404)])
        with self.assertRaisesRegex(SafetyError, "^PORTAL_OBJECT_NOT_FOUND$"):
            store.read_object(KEY)
        self.assertEqual(len(wire.calls), 1)
        store, _ = self.storage([response(b"ADMIN_OBJECT_UNAVAILABLE", 503)])
        with self.assertRaisesRegex(SafetyError, "^PORTAL_HTTP_REQUEST_REJECTED$"):
            store.read_object(KEY)

    def test_generic_or_changed_router_404_does_not_establish_object_absence(self):
        for raw in (b"Not Found", b"ADMIN_NOT_FOUND", b"ADMIN_OBJECT_NOT_FOUND\n",
                    b'<html>404 DEPLOYMENT_NOT_FOUND</html>', b'{"error":"ADMIN_OBJECT_NOT_FOUND"}'):
            with self.subTest(body=raw):
                store, wire = self.storage([response(raw, 404)])
                with self.assertRaisesRegex(SafetyError, "^PORTAL_HTTP_REQUEST_REJECTED$"):
                    store.read_object(KEY)
                self.assertEqual(len(wire.calls), 1)

    def config(self):
        return {"schema_version": 1, "origin": "https://dropshipper.neuraflow.co.in", "client_id": CLIENT,
                "service_token_id": "fixture-service-token", "service_client_id": SERVICE_ID,
                "service_client_secret": SERVICE_SECRET, "expires_at": "2027-10-08T10:00:00Z"}

    def test_factory_is_local_only_and_does_not_dispatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); wire = FakeTransport()
            atomic_json(root / "config/local/draft_automation.json", self.config())
            store = load_draft_automation_storage(root, transport=wire, as_of="2026-10-08T10:00:00Z")
            self.assertIsInstance(store, DraftAutomationStorage); self.assertEqual(wire.calls, [])

    def test_missing_expired_extra_or_changed_origin_credentials_are_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); path = root / "config/local/draft_automation.json"
            with self.assertRaises(SafetyError): load_draft_automation_storage(root)
            for edits in [{"schema_version": True}, {"expires_at": "2025-10-08T10:00:00Z"},
                          {"origin": "https://another.example"}, {"unexpected": "field"}, {"service_client_secret": "short"}]:
                atomic_json(path, self.config() | edits)
                with self.assertRaises(SafetyError) as caught: load_draft_automation_storage(root)
                self.assertNotIn(SERVICE_SECRET, str(caught.exception))

    def test_non_regular_and_symlink_config_never_block_or_read(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); path = root / "config/local/draft_automation.json"; path.parent.mkdir(parents=True)
            os.mkfifo(path)
            with self.assertRaises(SafetyError): load_draft_automation_storage(root)
            path.unlink(); original = root / "real.json"; atomic_json(original, self.config()); path.symlink_to(original)
            with self.assertRaises(SafetyError): load_draft_automation_storage(root)

    def test_callback_does_not_rewrite_the_helpers_locked_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); store, wire = self.storage()
            path = root / "data/outputs/issue-one/revision-1/private-staging-receipt.json"
            result = {"state": "private_bytes_verified", "package_hash": "f" * 64,
                      "approved": False, "publication_attempted": False, "index_write_attempted": False}
            atomic_json(path, result)
            original = path.read_bytes()
            with patch("product_radar.draft_automation.stage_private_draft", return_value=result) as stage:
                self.assertEqual(private_staging_callback(root, store)("issue-one", 1), result)
                stage.assert_called_once_with(root, "issue-one", 1, storage=store)
            self.assertEqual(path.read_bytes(), original)
            self.assertNotIn(SERVICE_SECRET.encode(), original); self.assertEqual(wire.calls, [])

    def test_waiting_callback_cannot_replace_a_successful_concurrent_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); store, wire = self.storage()
            path = root / "data/outputs/issue-one/revision-1/private-staging-receipt.json"
            saved = {"state": "private_bytes_verified", "package_hash": "f" * 64,
                     "approved": False, "publication_attempted": False, "index_write_attempted": False}
            atomic_json(path, saved); original = path.read_bytes()
            waiting = {"state": "storage_waiting", "reason": "PRIVATE_DRAFT_STAGING_ALREADY_RUNNING"}
            with patch("product_radar.draft_automation.stage_private_draft", return_value=waiting):
                self.assertEqual(private_staging_callback(root, store)("issue-one", 1), waiting)
            self.assertEqual(path.read_bytes(), original); self.assertEqual(wire.calls, [])


if __name__ == "__main__":
    unittest.main()
