"""Fixed private draft page protocol over injected transport, not cloud proof."""

from copy import deepcopy
import unittest

from product_radar.models import SafetyError
from product_radar.portal_storage import DraftAutomationStorage, PortalStorage, _validate_spec
from test_portal_storage import AT, CLIENT, ORIGIN, TOKEN, FakeTransport, response


def page():
    row = {"issue_id": "draft-one", "revision": 1, "product_count": 10, "package_hash": "a" * 64,
        "state": "stored_for_owner_review", "approved": False, "client_available": False,
        "files": [{"name": name, "size": 20, "sha256": "b" * 64,
            "url": f"/admin/download/draft-one/1/{name}"} for name in ("shortlist.xlsx", "report-en.pdf")]}
    return {"schema_version": 2, "contract": "private-draft-list-v2", "client_id": CLIENT,
        "source_prefix": f"clients/{CLIENT}/issues/", "index_hash": "c" * 64, "index_etag": None,
        "server_observed_at": AT, "drafts": [row], "skipped": [], "checked_versions": 1,
        "released_versions": 0, "scanned_versions": 1, "scanned_objects": 5, "source_pages": 1,
        "scan_limited": False, "next_cursor": None, "index_write_attempted": False, "publication_attempted": False}


class PrivateDraftPageTests(unittest.TestCase):
    def read(self, value, cursor=None):
        wire = FakeTransport([response(value)])
        storage = PortalStorage(ORIGIN, CLIENT, TOKEN, transport=wire)
        return storage.read_private_draft_page(cursor=cursor), wire

    def test_first_and_continuation_are_bounded_owner_gets(self):
        value = page()
        result, wire = self.read(value)
        self.assertEqual(result, value)
        self.assertEqual(wire.calls[0][:2], ("GET", ORIGIN + "/admin/draft-page"))
        value.update(next_cursor="saved_cursor", scan_limited=True)
        result, wire = self.read(value, cursor="previous_cursor")
        self.assertEqual(result["next_cursor"], "saved_cursor")
        self.assertTrue(wire.calls[0][1].endswith("?cursor=previous_cursor"))

    def test_wrong_scope_counts_or_cursor_never_becomes_complete_inventory(self):
        mutations = [{"client_id": "other"}, {"source_prefix": "clients/other/issues/"},
            {"scanned_versions": 9}, {"checked_versions": 0}, {"source_pages": 4},
            {"scanned_objects": 301}, {"next_cursor": "bad/url", "scan_limited": True},
            {"next_cursor": "okay", "scan_limited": False}, {"publication_attempted": True}]
        for change in mutations:
            with self.subTest(change=change), self.assertRaises(SafetyError): self.read(dict(page(), **change))

    def test_download_links_and_draft_flags_are_checked(self):
        for change in ({"approved": True}, {"client_available": True}, {"product_count": 0}, {"package_hash": "wrong"}):
            value = page(); value["drafts"][0].update(change)
            with self.subTest(change=change), self.assertRaises(SafetyError): self.read(value)
        value = page(); value["drafts"][0]["files"][0]["url"] = "https://another.example/private"
        with self.assertRaises(SafetyError): self.read(value)
        value = page(); value["drafts"].append(deepcopy(value["drafts"][0])); value.update(checked_versions=2, scanned_versions=2)
        with self.assertRaises(SafetyError): self.read(value)

    def test_unknown_query_duplicate_cursor_and_writes_are_refused(self):
        for path in ("/admin/draft-page?cursor=one&cursor=two", "/admin/draft-page?url=elsewhere", "/admin/draft-page?cursor=", "/admin/draft-page?cursor=bad%2Fvalue"):
            with self.subTest(path=path), self.assertRaises(SafetyError):
                _validate_spec(self.spec(path), b"")
        for method in ("PUT", "POST", "DELETE"):
            spec = self.spec("/admin/draft-page"); spec["method"] = method
            with self.subTest(method=method), self.assertRaises(SafetyError): _validate_spec(spec, b"")

    def test_machine_draft_key_does_not_gain_owner_inventory_permission(self):
        wire = FakeTransport([])
        storage = DraftAutomationStorage(ORIGIN, CLIENT, "a" * 32 + ".access", "b" * 64, transport=wire)
        with self.assertRaisesRegex(SafetyError, "PORTAL_DRAFT_ONLY_ROUTE_REQUIRED"):
            storage.read_private_draft_page()
        self.assertEqual(wire.calls, [])

    @staticmethod
    def spec(path):
        return {"method": "GET", "url": ORIGIN + path, "max_response_bytes": 512 * 1024,
            "headers": {"cf-access-token": TOKEN, "X-NeuraFlow-Release-Action": "v1", "Accept-Encoding": "identity"}}
