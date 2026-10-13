"""Manufactured packet and private fake storage only; no genuine research claim."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.delivery_package import write_delivery_manifest
from product_radar.models import SafetyError
from product_radar.private_drafts import prepare_private_draft, stage_private_draft, verify_owner_draft_downloads
from product_radar.storage import atomic_bytes, atomic_json, load_json
from test_delivery_package import packet
from test_live_outputs import fixture


class Store:
    def __init__(self):
        self.objects, self.calls = {}, []
        self.fail_name, self.lost_name, self.corrupt_name = None, None, None
        self.fail_after_index = False
        self.index_reads = 0
    def read_index(self, key):
        self.calls.append(("index_get", key))
        self.index_reads += 1
        if self.fail_after_index and self.index_reads > 1:
            raise SafetyError("PORTAL_REQUEST_FAILED")
        return {"etag": None, "index": {"issues": []}}
    def put_immutable(self, key, raw, **kwargs):
        self.calls.append(("put", key))
        name = key.rsplit("/", 1)[-1]
        if name == self.fail_name:
            raise SafetyError("PORTAL_HTTP_REQUEST_REJECTED")
        if key in self.objects and self.objects[key] != raw:
            raise SafetyError("PORTAL_HTTP_REQUEST_REJECTED")
        self.objects[key] = raw
        if name == self.lost_name:
            raise SafetyError("PORTAL_REQUEST_FAILED_RECONCILE")
    def read_object(self, key):
        self.calls.append(("get", key))
        if key not in self.objects:
            raise SafetyError("PORTAL_OBJECT_NOT_FOUND")
        return b"wrong" if key.rsplit("/", 1)[-1] == self.corrupt_name else self.objects[key]
    def read_private_draft_list(self):
        self.calls.append(("owner_page", "/admin/drafts"))
        prefix = f"clients/{self.client_id}/issues/{self.issue_id}/r{self.revision}/"
        return "".join(f'<a href="/admin/download/{self.issue_id}/{self.revision}/{name}">{name}</a>'
                       for name in ("shortlist.xlsx", "report-en.pdf", "report-hi-Latn.pdf") if prefix + name in self.objects)
    def read_private_draft_download(self, key, **kwargs):
        self.calls.append(("owner_download", key))
        return b"wrong" if key.rsplit("/", 1)[-1] == self.corrupt_name else self.objects[key]


class PrivateDraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        def captured_wire_fixture(*args, **kwargs):
            # Exercise the captured-data contract with disposable fictional
            # facts, as in the existing Worker tests. Never saved in the app.
            issue, data = fixture(*args, **kwargs)
            data["evidence_type"] = "captured"
            return issue, data
        with patch("test_delivery_package.fixture", side_effect=captured_wire_fixture):
            self.issue, data, drafts, receipts = packet(self.root)
        self.manifest = write_delivery_manifest(self.issue, drafts, receipts, root=self.root)
        self.folder = self.root / "data/outputs" / self.issue["issue_id"] / ("revision-" + str(self.issue["revision"]))
        atomic_json(self.folder / "package-input.json", {"issue": self.issue})
    def stage(self, store=None):
        return stage_private_draft(self.root, self.issue["issue_id"], self.issue["revision"], storage=store)
    def test_prepare_rechecks_real_files_without_storage_or_publication(self):
        result = self.stage()
        self.assertEqual(result["state"], "prepared")
        self.assertEqual(len(result["files"]), 5)
        self.assertNotIn("raw", str(result))
        self.assertFalse(result["publication_attempted"])
    def test_manifest_is_uploaded_last_and_all_bytes_downloaded_before_it(self):
        store = Store(); result = self.stage(store)
        self.assertEqual(result["state"], "private_bytes_verified")
        self.assertEqual(result["verified_file_count"], 5)
        self.assertTrue(result["index_unchanged"])
        puts = [key for action, key in store.calls if action == "put"]
        self.assertEqual(len(puts), 5)
        self.assertTrue(puts[-1].endswith("delivery-manifest.json"))
        self.assertEqual(len([call for call in store.calls if call[0] == "get"]), 5)
        self.assertFalse(any(action == "index_put" for action, key in store.calls))
    def test_lost_upload_reply_reuses_one_exact_download_without_second_put(self):
        store = Store(); store.lost_name = "report-en.pdf"
        result = self.stage(store)
        self.assertEqual(result["state"], "private_bytes_verified")
        row = next(row for row in result["outcomes"] if row["name"] == store.lost_name)
        self.assertTrue(row["recovered_write_reply"])
        self.assertEqual(len([key for action, key in store.calls if action == "put" and key.endswith(store.lost_name)]), 1)
    def test_one_failed_file_does_not_stop_other_files_or_create_manifest(self):
        store = Store(); store.fail_name = "report-en.pdf"
        result = self.stage(store)
        self.assertEqual(result["state"], "work_remaining")
        self.assertEqual(result["verified_file_count"], 3)
        self.assertTrue(any(key.endswith("report-hi-Latn.pdf") for action, key in store.calls if action == "put"))
        self.assertFalse(any(key.endswith("delivery-manifest.json") for action, key in store.calls if action == "put"))
        self.assertEqual(result["outcomes"][-1]["state"], "not_uploaded")
    def test_wrong_remote_bytes_are_not_marked_verified(self):
        store = Store(); store.corrupt_name = "shortlist.xlsx"
        result = self.stage(store)
        self.assertEqual(result["state"], "work_remaining")
        self.assertEqual(result["outcomes"][0]["reason"], "PRIVATE_DRAFT_READBACK_MISMATCH")
    def test_unconfirmed_after_index_retains_file_receipts_without_claiming_done(self):
        store = Store(); store.fail_after_index = True
        result = self.stage(store)
        self.assertEqual(result["state"], "index_check_waiting")
        self.assertEqual(result["verified_file_count"], 5)
        self.assertIsNone(result["index_unchanged"])
    def test_changed_local_file_is_rejected_before_any_upload(self):
        atomic_bytes(self.folder / "shortlist.xlsx", b"PK changed")
        store = Store()
        with self.assertRaises(SafetyError):
            self.stage(store)
        self.assertEqual(store.calls, [])
    def test_approved_or_incomplete_manifest_cannot_enter_private_draft_route(self):
        for changes in ({"approved": True}, {"complete": False}, {"release_ready": True}, {"evidence_type": "synthetic"}):
            atomic_json(self.folder / "delivery-manifest.json", dict(self.manifest, **changes))
            with self.assertRaisesRegex(SafetyError, "PRIVATE_DRAFT_COMPLETE_UNAPPROVED_PACKET_REQUIRED"):
                self.stage(Store())
    def test_replay_keeps_exact_existing_bytes_and_never_publishes(self):
        store = Store(); self.stage(store)
        original = deepcopy(store.objects)
        original_calls = list(store.calls)
        self.assertEqual(self.stage(store)["state"], "private_bytes_verified")
        self.assertEqual(store.objects, original)
        self.assertEqual(store.calls, original_calls)
    def test_server_error_after_committed_write_is_read_back_without_a_second_put(self):
        class ServerError(Store):
            def put_immutable(self, key, raw, **kwargs):
                super().put_immutable(key, raw, **kwargs)
                if key.endswith("report-en.pdf"):
                    raise SafetyError("PORTAL_HTTP_REQUEST_REJECTED")
        store = ServerError(); result = self.stage(store)
        self.assertEqual(result["state"], "private_bytes_verified")
        english = next(row for row in result["outcomes"] if row["name"] == "report-en.pdf")
        self.assertTrue(english["recovered_write_reply"])
        self.assertEqual(sum(kind == "put" and key.endswith("report-en.pdf") for kind, key in store.calls), 1)
    def test_restart_reads_uncertain_committed_pdf_and_reuses_other_finished_files(self):
        class LostRead(Store):
            unavailable = True
            def read_object(self, key):
                if self.unavailable and key.endswith("report-en.pdf"):
                    self.calls.append(("get", key)); raise SafetyError("PORTAL_REQUEST_FAILED")
                return super().read_object(key)
        store = LostRead(); store.lost_name = "report-en.pdf"
        first = self.stage(store); self.assertEqual(first["state"], "work_remaining")
        puts_before = sum(kind == "put" for kind, _ in store.calls)
        store.unavailable = False
        result = self.stage(store); self.assertEqual(result["state"], "private_bytes_verified")
        self.assertEqual(sum(kind == "put" for kind, _ in store.calls), puts_before + 1)  # missing manifest only
        self.assertEqual(sum(kind == "put" and key.endswith("report-en.pdf") for kind, key in store.calls), 1)
        self.assertTrue(next(row for row in result["outcomes"] if row["name"] == "report-en.pdf")["recovered_write_reply"])
    def test_original_intent_can_put_again_only_after_authenticated_missing_object(self):
        store = Store(); store.fail_name = "report-en.pdf"
        self.assertEqual(self.stage(store)["state"], "work_remaining")
        store.fail_name = None; start = len(store.calls)
        self.assertEqual(self.stage(store)["state"], "private_bytes_verified")
        calls = [kind for kind, key in store.calls[start:] if key.endswith("report-en.pdf")]
        self.assertEqual(calls, ["get", "put", "get"])
    def test_unavailable_reconciliation_does_not_send_another_uncertain_put(self):
        class NoRead(Store):
            def read_object(self, key):
                if key.endswith("report-en.pdf"):
                    self.calls.append(("get", key)); raise SafetyError("PORTAL_REQUEST_FAILED")
                return super().read_object(key)
        store = NoRead(); self.stage(store)
        for _ in range(2): self.assertEqual(self.stage(store)["state"], "work_remaining")
        self.assertEqual(sum(kind == "put" and key.endswith("report-en.pdf") for kind, key in store.calls), 1)
    def test_changed_intent_binding_is_refused_before_any_external_request(self):
        from product_radar.storage import load_json
        store = Store(); store.fail_name = "report-en.pdf"; self.stage(store)
        path = self.folder / "private-staging-control.json"; saved = load_json(path)
        saved["files"]["report-en.pdf"]["sha256"] = "f" * 64; atomic_json(path, saved)
        before = list(store.calls)
        with self.assertRaisesRegex(SafetyError, "PRIVATE_DRAFT_STAGING_BINDING_CHANGED"):
            self.stage(store)
        self.assertEqual(store.calls, before)
    def test_lost_final_receipt_save_reuses_per_file_proofs_without_upload(self):
        import product_radar.private_drafts as module
        real = module.atomic_json
        def fail_receipt(path, value):
            if Path(path).name == "private-staging-receipt.json": raise OSError("fixture save failed")
            return real(path, value)
        store = Store()
        with patch.object(module, "atomic_json", side_effect=fail_receipt), self.assertRaises(OSError):
            self.stage(store)
        before = sum(kind == "put" for kind, _ in store.calls)
        self.assertEqual(self.stage(store)["state"], "private_bytes_verified")
        self.assertEqual(sum(kind == "put" for kind, _ in store.calls), before)
    def owner_store(self):
        store = Store(); self.stage(store)
        store.client_id, store.issue_id, store.revision = self.issue["client_id"], self.issue["issue_id"], self.issue["revision"]
        store.calls.clear()
        return store
    def verify_owner(self, store):
        return verify_owner_draft_downloads(self.root, self.issue["issue_id"], self.issue["revision"], storage=store)
    def test_owner_page_and_three_downloads_are_read_back_without_any_write(self):
        store = self.owner_store(); result = self.verify_owner(store)
        self.assertEqual(result["state"], "owner_downloads_verified")
        self.assertEqual(result["verified_download_count"], 3)
        self.assertTrue(result["index_unchanged"])
        self.assertFalse(result["upload_attempted"])
        self.assertEqual(sum(action == "owner_download" for action, _ in store.calls), 3)
        self.assertFalse(any(action == "put" for action, _ in store.calls))
    def test_missing_link_does_not_prevent_other_owner_downloads(self):
        store = self.owner_store()
        del store.objects[next(key for key in store.objects if key.endswith("report-en.pdf"))]
        result = self.verify_owner(store)
        self.assertEqual(result["state"], "work_remaining")
        self.assertEqual(result["verified_download_count"], 2)
        self.assertEqual(result["outcomes"][1]["reason"], "PRIVATE_DRAFT_DOWNLOAD_LINK_MISSING")
    def test_changed_owner_download_is_not_marked_verified(self):
        store = self.owner_store(); store.corrupt_name = "report-en.pdf"
        result = self.verify_owner(store)
        self.assertEqual(result["verified_download_count"], 2)
        self.assertEqual(result["outcomes"][1]["reason"], "PRIVATE_DRAFT_READBACK_MISMATCH")

    def draft_page(self, store, *, found=False, next_cursor=None):
        from product_radar.models import digest
        index = store.read_index(f"clients/{self.manifest['client_id']}/release-index.json")
        drafts = [{"issue_id": self.issue["issue_id"], "revision": self.issue["revision"],
            "package_hash": self.manifest["package_hash"], "product_count": self.manifest["product_count"],
            "files": [{"name": item["name"], "sha256": item["sha256"], "size": item["size"],
                "url": f"/admin/download/{self.issue['issue_id']}/{self.issue['revision']}/{item['name']}"}
                for item in self.manifest["files"]]}] if found else []
        return {"client_id": self.manifest["client_id"], "index_etag": index["etag"],
                "index_hash": digest(index["index"]), "drafts": drafts, "next_cursor": next_cursor}

    def test_owner_download_search_resumes_saved_bounded_pages(self):
        store = self.owner_store(); cursors = []
        def read_page(*, cursor=None):
            cursors.append(cursor)
            return self.draft_page(store, found=cursor == "later_page", next_cursor="later_page" if cursor is None else None)
        store.read_private_draft_page = read_page
        first = verify_owner_draft_downloads(self.root, self.issue["issue_id"], self.issue["revision"], storage=store, max_pages=1)
        self.assertEqual(first["state"], "owner_page_search_continuation")
        self.assertEqual(first["owner_pages_checked"], 1)
        self.assertFalse(any(action == "owner_download" for action, _ in store.calls))
        again = self.verify_owner(store)
        self.assertEqual(again["state"], "owner_downloads_verified")
        self.assertEqual(cursors, [None, "later_page"])

    def test_stale_storage_cursor_is_cleared_before_next_attempt(self):
        store = self.owner_store(); cursors = []
        store.read_private_draft_page = lambda **_: self.draft_page(store, next_cursor="stale_page")
        verify_owner_draft_downloads(self.root, self.issue["issue_id"], self.issue["revision"], storage=store, max_pages=1)
        def failing(*, cursor=None):
            cursors.append(cursor)
            raise SafetyError("PORTAL_HTTP_REQUEST_REJECTED")
        store.read_private_draft_page = failing
        self.verify_owner(store)
        store.read_private_draft_page = lambda **options: (cursors.append(options.get("cursor")) or self.draft_page(store, found=True))
        self.assertEqual(self.verify_owner(store)["state"], "owner_downloads_verified")
        self.assertEqual(cursors, ["stale_page", None])

    def test_page_cycle_never_spins_or_claims_a_download(self):
        store = self.owner_store()
        store.read_private_draft_page = lambda **_: self.draft_page(store, next_cursor="same_page")
        result = self.verify_owner(store)
        self.assertFalse(result["owner_page_verified"])
        self.assertEqual(result["owner_pages_checked"], 2)
        self.assertFalse(any(action == "owner_download" for action, _ in store.calls))

    def test_malformed_disposable_cursor_cache_cannot_block_a_valid_draft(self):
        store = self.owner_store(); cursors = []
        store.read_private_draft_page = lambda **options: (cursors.append(options.get("cursor")) or self.draft_page(store, found=True))
        path = self.folder / "private-owner-page-progress.json"
        self.assertEqual(self.verify_owner(store)["state"], "owner_downloads_verified")
        valid = load_json(path, 16 * 1024)
        for invalid in (["not-a-record"], dict(valid, next_cursor=[]), dict(valid, schema_version=True),
                        dict(valid, next_cursor="bad/url"), dict(valid, next_cursor="x" * 3073)):
            atomic_json(path, invalid)
            with self.subTest(invalid=type(invalid).__name__):
                self.assertEqual(self.verify_owner(store)["state"], "owner_downloads_verified")
                self.assertIsNone(cursors[-1])
        atomic_bytes(path, b"not valid json")
        self.assertEqual(self.verify_owner(store)["state"], "owner_downloads_verified")
        self.assertIsNone(cursors[-1])


if __name__ == "__main__":
    unittest.main()
