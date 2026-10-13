"""Actual page bytes over an injected owner transport; no hosted writes."""

from copy import deepcopy
import hashlib
import unittest
from urllib.parse import parse_qs, urlsplit

from product_radar.models import SafetyError, canonical, digest
from product_radar.portal_storage import (DraftAutomationStorage, PortalResponse, PortalStorage,
    MAX_INDEX, PortalOwnerActionRequired, _perform_raw, _validate_spec)
from product_radar.release_pages import (ReleasePages, empty_release_root, read_release_cursor,
    release_cursor, sequence_key)
from test_portal_storage import ORIGIN, TOKEN, AT, CLIENT, response
from test_release_pages import Store


class PageWire:
    def __init__(self, tree, objects):
        self.root, self.objects, self.calls = deepcopy(tree.root), objects, []
        self.forge = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        _validate_spec({"method": method, "url": url, "headers": kwargs["headers"],
            "max_response_bytes": kwargs["max_response_bytes"]}, kwargs["body"])
        parsed = urlsplit(url)
        snapshot = {"index": self.root, "etag": "v2-original", "server_observed_at": AT,
                    "verified": True, "authority": "release_index"}
        if parsed.path == "/admin/index": return response(snapshot)
        if parsed.path.startswith("/admin/release-pages/"):
            suffix = parsed.path[len("/admin/release-pages/"):]
            raw = self.objects[f"clients/{CLIENT}/release-index-v2/pages/{suffix}"]
            return PortalResponse(200, {"content-type": "application/json", "content-length": str(len(raw)),
                "x-content-sha256": hashlib.sha256(raw).hexdigest()}, raw)
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        kind = params["kind"]
        tree = ReleasePages(self.root, self.objects.__getitem__)
        if "id" in params:
            key = digest(params["id"]) + (sequence_key(int(params["revision"])) if kind == "revisions" else "")
            value = tree.get(kind, key)
            if kind == "revisions" and value:
                value = tree.get("archive", sequence_key(value["sequence"]))
            return response({**snapshot, "kind": kind, "value": value})
        cursor = read_release_cursor(params["cursor"], tree.root, kind) if "cursor" in params else None
        reverse = kind in {"archive", "timeline"}
        page = tree.scan(kind, after=cursor["last_key"] if cursor else None, reverse=reverse, limit=20)
        token = release_cursor(tree.root, kind, page["last_key"], reverse=reverse) if page["more"] else None
        rows = deepcopy(page["rows"])
        if self.forge and rows: rows[0]["value"]["sequence"] = 99999
        return response({**snapshot, "kind": kind, "rows": rows, "next_cursor": token})


class PortalReleasePageTests(unittest.TestCase):
    def setUp(self):
        self.objects = Store()
        self.tree = ReleasePages(empty_release_root(CLIENT, AT), self.objects.read, self.objects.put)
        archive = [{"key": sequence_key(n), "value": {"issue_id": "issue-" + str(n), "revision": 1, "sequence": n}}
                   for n in range(1, 46)]
        revisions = sorted([{"key": digest(r["value"]["issue_id"]) + sequence_key(1),
                             "value": r["value"]} for r in archive], key=lambda row: row["key"])
        self.tree.root["pages"]["archive"] = self.tree.build("archive", archive)
        self.tree.root["pages"]["revisions"] = self.tree.build("revisions", revisions)
        self.tree.root["next_sequence"] = 46
        self.tree.persist()
        self.wire = PageWire(self.tree, self.objects.objects)
        self.storage = PortalStorage(ORIGIN, CLIENT, TOKEN, transport=self.wire)

    def test_owner_pages_use_actual_hash_checked_objects_and_current_root_cursor(self):
        first = self.storage.read_index_page("archive")
        self.assertEqual([r["value"]["sequence"] for r in first["rows"]], list(range(45, 25, -1)))
        second = self.storage.read_index_page("archive", cursor=first["next_cursor"])
        self.assertEqual([r["value"]["sequence"] for r in second["rows"]], list(range(25, 5, -1)))
        self.assertTrue(all(method == "GET" for method, _, _ in self.wire.calls))

    def test_exact_old_revision_is_checked_against_original_archive_bytes(self):
        result = self.storage.read_index_page("revisions", item_id="issue-1", revision=1)
        self.assertEqual(result["value"], {"issue_id": "issue-1", "revision": 1, "sequence": 1})
        self.assertIsNone(self.storage.read_index_page("revisions", item_id="missing", revision=1)["value"])

    def test_forged_page_rows_are_not_accepted_as_the_history(self):
        self.wire.forge = True
        with self.assertRaisesRegex(SafetyError, "PORTAL_HISTORY_RESPONSE_INVALID"):
            self.storage.read_index_page("archive")

    def test_fixed_queries_refuse_duplicate_or_unknown_inputs_and_writes(self):
        paths = ["/admin/index?kind=archive", "/admin/index-page?kind=archive&kind=types",
            "/admin/index-page?kind=archive&url=https%3A%2F%2Fevil.example",
            "/admin/index-page?kind=archive&id=one", "/admin/index-page?kind=revisions&id=one",
            "/admin/index-page?kind=revisions&id=one&revision=0",
            "/admin/index-page?kind=types&id=one&cursor=anything",
            "/_automation/drafts/index?kind=types"]
        for path in paths:
            spec = self.spec(path)
            if path.startswith("/_automation"):
                spec["headers"].pop("cf-access-token")
                spec["headers"].update({"CF-Access-Client-Id": "a" * 32 + ".access", "CF-Access-Client-Secret": "b" * 64})
            with self.subTest(path=path), self.assertRaises(SafetyError): _validate_spec(spec, b"")
        for path in ["/admin/index-page?kind=archive", "/admin/release-pages/archive/" + "a" * 64 + ".json"]:
            for method in ("POST", "PUT", "DELETE"):
                spec = self.spec(path); spec["method"] = method
                with self.subTest(method=method, path=path), self.assertRaises(SafetyError): _validate_spec(spec, b"{}")

    @staticmethod
    def spec(path):
        return {"method": "GET", "url": ORIGIN + path, "max_response_bytes": MAX_INDEX,
            "headers": {"cf-access-token": TOKEN, "X-NeuraFlow-Release-Action": "v1", "Accept-Encoding": "identity"}}

    def test_draft_only_key_cannot_use_owner_history_or_v2_mutations(self):
        draft = DraftAutomationStorage(ORIGIN, CLIENT, "a" * 32 + ".access", "b" * 64, transport=self.wire)
        ref = self.tree.root["pages"]["archive"]
        operations = [lambda: draft.read_index_page("archive"), lambda: draft.read_object(ref["key"]),
            lambda: draft.compare_and_swap_index_v2(f"clients/{CLIENT}/release-index.json", "v2-original",
                {"kind": "reconcile", "client_id": CLIENT})]
        for action in operations:
            with self.assertRaisesRegex(SafetyError, "PORTAL_DRAFT_ONLY_ROUTE_REQUIRED"): action()
        self.assertEqual(self.wire.calls, [])

    def test_default_https_operation_preserves_validated_query_in_request_target(self):
        class Connection:
            seen = None
            def __init__(self, *args, **kwargs): pass
            def request(self, method, path, **kwargs): Connection.seen = path
            def getresponse(self): return self
            status = 200
            def getheaders(self): return [("Content-Type", "application/json"), ("Content-Length", "2")]
            def read(self, limit): return b"{}"
            def close(self): pass
        _perform_raw(self.spec("/admin/index-page?kind=types&id=one"), b"", connection_factory=Connection)
        self.assertEqual(Connection.seen, "/admin/index-page?kind=types&id=one")

    def test_history_page_objects_are_read_only_even_with_the_owner_key(self):
        ref = self.tree.root["pages"]["archive"]
        raw = self.objects.objects[ref["key"]]
        with self.assertRaisesRegex(SafetyError, "PORTAL_OBJECT_KEY_INVALID"):
            self.storage.put_immutable(ref["key"], raw, sha256=ref["sha256"], media_type="application/json")
        self.assertEqual(self.wire.calls, [])

    def test_exact_owner_action_details_survive_without_logging_a_server_body(self):
        from test_portal_storage import FakeTransport
        wire = FakeTransport([response({"error": "ADMIN_ALIAS_QUEUE_OWNER_ACTION_REQUIRED",
            "affected_issue_ids": ["issue-1", "issue-2"], "next_available_at": AT,
            "action": "Private server explanation not copied into the error message."}, status=409)])
        storage = PortalStorage(ORIGIN, CLIENT, TOKEN, transport=wire)
        with self.assertRaises(PortalOwnerActionRequired) as caught:
            storage.compare_and_swap_index_v2(f"clients/{CLIENT}/release-index.json", "v2-original",
                {"kind": "reconcile", "client_id": CLIENT})
        self.assertEqual(str(caught.exception), "PORTAL_OWNER_ACTION_REQUIRED")
        self.assertEqual(caught.exception.affected_issue_ids, ("issue-1", "issue-2"))
        self.assertEqual(caught.exception.next_available_at, AT)

    def test_malformed_owner_action_is_not_a_trusted_repair_list(self):
        from test_portal_storage import FakeTransport
        for ids in (["issue-1", "issue-1"], ["../private"], [True], "all issues"):
            wire = FakeTransport([response({"error": "ADMIN_QUEUE_OWNER_ACTION_REQUIRED",
                "affected_issue_ids": ids}, status=409)])
            storage = PortalStorage(ORIGIN, CLIENT, TOKEN, transport=wire)
            with self.subTest(ids=ids), self.assertRaisesRegex(SafetyError, "^PORTAL_HTTP_REQUEST_REJECTED$"):
                storage.compare_and_swap_index_v2(f"clients/{CLIENT}/release-index.json", "v2-original",
                    {"kind": "reconcile", "client_id": CLIENT})


if __name__ == "__main__": unittest.main()
