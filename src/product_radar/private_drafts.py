"""Verify and stage a real complete draft for owner review, never publication.

The existing packet validator remains the local content authority. Files are
immutable and downloaded byte-for-byte before the manifest is staged last.
An uncertain upload is read back, never blindly repeated. Separate file
failures are recorded and healthy files continue. No release index is written.
"""

from pathlib import Path
from html.parser import HTMLParser
import re

from .delivery_package import _artifact, _read, validate_delivery_manifest
from .live_outputs import digest_bytes
from .models import SafetyError, canonical, digest, identifier, utc_now
from .runtime_storage import runtime_path
from .storage import Journal, atomic_json, load_json, safe_path


def prepare_private_draft(root, issue_id, revision):
    root = safe_path(root).resolve()
    identifier(issue_id)
    if type(revision) is not int or not 1 <= revision <= 9007199254740991:
        raise SafetyError("PRIVATE_DRAFT_REVISION_INVALID")
    folder = safe_path(runtime_path(root, "data/outputs"), issue_id, "revision-" + str(revision))
    binding = load_json(folder / "package-input.json", 8 * 1024 * 1024)
    manifest = load_json(folder / "delivery-manifest.json", 512 * 1024)
    issue = binding.get("issue") if isinstance(binding, dict) else None
    if (not isinstance(issue, dict) or issue.get("issue_id") != issue_id or issue.get("revision") != revision
            or not isinstance(manifest, dict) or manifest.get("evidence_type") != "captured"
            or manifest.get("complete") is not True or manifest.get("approved") is not False
            or manifest.get("release_ready") is not False or manifest.get("state") != "draft_ready_for_review"):
        raise SafetyError("PRIVATE_DRAFT_COMPLETE_UNAPPROVED_PACKET_REQUIRED")
    validate_delivery_manifest(manifest, issue, root=root)
    client = identifier(manifest["client_id"])
    prefix = f"clients/{client}/issues/{issue_id}/r{revision}/"
    files = []
    for entry in manifest["files"]:
        name = entry["name"]
        artifact = manifest["artifacts"]["workbook" if name == "shortlist.xlsx" else "pdf-" + name[7:-4]]
        raw = _read(artifact["path"])
        if digest_bytes(raw) != entry["sha256"] or len(raw) != entry["size"] or entry["key"] != prefix + name:
            raise SafetyError("PRIVATE_DRAFT_FILE_CHANGED")
        files.append(dict(entry, raw=raw))
    facts_path = folder / "final_report_data.json"
    facts, raw = _artifact(manifest["artifacts"]["facts"], facts_path, 4 * 1024 * 1024)
    files.append({"name": "final_report_data.json", "key": prefix + "final_report_data.json", "media_type": "application/json",
                  "sha256": facts["sha256"], "size": len(raw), "raw": raw})
    raw = _read(folder / "delivery-manifest.json", 512 * 1024)
    if raw != canonical(manifest) + b"\n":
        raise SafetyError("PRIVATE_DRAFT_MANIFEST_BYTES_CHANGED")
    files.append({"name": "delivery-manifest.json", "key": prefix + "delivery-manifest.json", "media_type": "application/json",
                  "sha256": digest_bytes(raw), "size": len(raw), "raw": raw})
    return manifest, files


def _reason(error):
    allowed = {"PORTAL_OPERATOR_ACCESS_REQUIRED", "PORTAL_REDIRECT_REFUSED", "PORTAL_REQUEST_FAILED",
               "PORTAL_REQUEST_FAILED_RECONCILE", "PORTAL_HTTP_REQUEST_REJECTED", "PORTAL_OBJECT_VERIFICATION_FAILED",
               "PORTAL_UPLOAD_RECEIPT_INVALID", "PORTAL_OBJECT_NOT_FOUND", "PRIVATE_DRAFT_READBACK_MISMATCH"}
    return str(error) if isinstance(error, SafetyError) and str(error) in allowed else "PRIVATE_DRAFT_STORAGE_CHECK_FAILED"


def _verified_receipt(value, expected):
    if (not isinstance(value, dict) or value.get("state") != "private_bytes_verified" or value.get("index_unchanged") is not True
            or canonical({name: value.get(name) for name in expected}) != canonical(expected)):
        return False
    rows = value.get("outcomes")
    if (not isinstance(rows, list) or len(rows) != len(expected["files"]) or any(not isinstance(row, dict) for row in rows)
            or value.get("verified_file_count") != len(rows)):
        return False
    by_name = {row.get("name"): row for row in rows}
    return len(by_name) == len(rows) and all(
        row.get("state") == "verified" and type(row.get("size")) is int and row.get("size") == item["size"]
        and row.get("sha256") == item["sha256"]
        for item in expected["files"] for row in [by_name.get(item["name"], {})])


def stage_private_draft(root, issue_id, revision, *, storage=None):
    manifest, files = prepare_private_draft(root, issue_id, revision)
    receipt = {"schema_version": 1, "state": "prepared", "purpose": "private_owner_draft_review",
               "issue_id": issue_id, "revision": revision, "client_id": manifest["client_id"],
               "product_count": manifest["product_count"], "package_hash": manifest["package_hash"],
               "approved": False, "release_ready": False, "client_available": False,
               "index_write_attempted": False, "publication_attempted": False,
               "files": [{key: value for key, value in item.items() if key != "raw"} for item in files]}
    if storage is None:
        return receipt
    folder = safe_path(runtime_path(root, "data/outputs"), issue_id, "revision-" + str(revision))
    try:
        # Only this issue's storage writes are serialized. This is not the
        # research or money lock; other research and other drafts can proceed.
        with Journal(folder / "private_staging").locked(timeout=5):
            return _stage_files(folder, manifest, files, receipt, storage)
    except SafetyError as error:
        if str(error) == "RUNNER_ALREADY_ACTIVE":
            return dict(receipt, state="storage_waiting", reason="PRIVATE_DRAFT_STAGING_ALREADY_RUNNING", outcomes=[])
        raise


def _stage_files(folder, manifest, files, receipt, storage):
    expected = {key: value for key, value in receipt.items() if key != "state"}
    receipt_path = folder / "private-staging-receipt.json"
    control_path = folder / "private-staging-control.json"
    if receipt_path.exists():
        saved = load_json(receipt_path, 256 * 1024)
        if _verified_receipt(saved, expected):
            return dict(saved, reused_private_staging=True)
    plan_hash = digest(expected)
    control = {"schema_version": 1, "plan_hash": plan_hash, "files": {}}
    if control_path.exists():
        control = load_json(control_path, 256 * 1024)
        if (not isinstance(control, dict) or type(control.get("schema_version")) is not int or control.get("schema_version") != 1 or control.get("plan_hash") != plan_hash
                or not isinstance(control.get("files"), dict) or set(control["files"]) - {item["name"] for item in files}):
            raise SafetyError("PRIVATE_DRAFT_STAGING_BINDING_CHANGED")
        for item in files:
            prior = control["files"].get(item["name"])
            if prior is not None and (not isinstance(prior, dict) or prior.get("state") not in {"intent_saved", "verified"}
                    or canonical({key: prior.get(key) for key in ("key", "sha256", "size")}) != canonical({key: item[key] for key in ("key", "sha256", "size")})):
                raise SafetyError("PRIVATE_DRAFT_STAGING_BINDING_CHANGED")
    index_key = f"clients/{manifest['client_id']}/release-index.json"
    try:
        before = storage.read_index(index_key)
    except Exception as error:
        return dict(receipt, state="storage_waiting", reason=_reason(error), outcomes=[])
    outcomes = []
    for item in files:
        if item["name"] == "delivery-manifest.json" and any(row["state"] != "verified" for row in outcomes):
            outcomes.append({"name": item["name"], "state": "not_uploaded", "reason": "PRIVATE_DRAFT_FILES_NEED_VERIFICATION"})
            continue
        recovered = False
        try:
            prior = control["files"].get(item["name"])
            if prior and prior["state"] == "verified":
                # Exact immutable bytes were already read back. Their proof is
                # reused, not presented as a new storage availability check.
                outcomes.append({"name": item["name"], "state": "verified", "size": item["size"],
                                 "sha256": item["sha256"], "recovered_write_reply": False, "reused_file_proof": True})
                continue
            checked = None
            if prior:
                try:
                    checked = storage.read_object(item["key"])
                    recovered = True
                except SafetyError as error:
                    if str(error) != "PORTAL_OBJECT_NOT_FOUND":
                        raise
                    # Only an actual authenticated 404 establishes that the
                    # original immutable write did not leave this object.
            if checked is None:
                control["files"][item["name"]] = {"state": "intent_saved", "key": item["key"],
                                                   "sha256": item["sha256"], "size": item["size"]}
                atomic_json(control_path, control)
                try:
                    storage.put_immutable(item["key"], item["raw"], sha256=item["sha256"], media_type=item["media_type"])
                except Exception:
                    # A transport timeout or server rejection after a PUT may
                    # follow a committed write. Read once; never PUT twice.
                    recovered = True
                checked = storage.read_object(item["key"])
            if checked != item["raw"] or digest_bytes(checked) != item["sha256"]:
                raise SafetyError("PRIVATE_DRAFT_READBACK_MISMATCH")
            control["files"][item["name"]]["state"] = "verified"
            atomic_json(control_path, control)
            outcomes.append({"name": item["name"], "state": "verified", "recovered_write_reply": recovered,
                             "size": item["size"], "sha256": item["sha256"]})
        except Exception as error:
            outcomes.append({"name": item["name"], "state": "needs_follow_up", "reason": _reason(error)})
    receipt.update(outcomes=outcomes, verified_file_count=sum(row["state"] == "verified" for row in outcomes),
                   state="private_bytes_verified" if all(row["state"] == "verified" for row in outcomes) else "work_remaining")
    try:
        after = storage.read_index(index_key)
        unchanged = before["etag"] == after["etag"] and before["index"] == after["index"]
        receipt["index_unchanged"] = unchanged
        if not unchanged:
            receipt.update(state="index_changed_elsewhere", reason="PRIVATE_DRAFT_INDEX_CHANGED")
    except Exception as error:
        receipt.update(state="index_check_waiting", reason=_reason(error), index_unchanged=None)
    receipt["checked_at"] = utc_now()
    atomic_json(receipt_path, receipt)
    return receipt


class _DraftLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = set()

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.update(value for key, value in attrs if key == "href" and isinstance(value, str))


def verify_owner_draft_downloads(root, issue_id, revision, *, storage, max_pages=8):
    """Read bounded owner pages and exact links; no remote upload or release."""
    if type(max_pages) is not int or not 1 <= max_pages <= 20:
        raise SafetyError("PRIVATE_DRAFT_PAGE_LIMIT_INVALID")
    manifest, files = prepare_private_draft(root, issue_id, revision)
    receipt = {"schema_version": 1, "state": "work_remaining", "purpose": "private_owner_download_check",
               "issue_id": issue_id, "revision": revision, "client_id": manifest["client_id"],
               "package_hash": manifest["package_hash"], "product_count": manifest["product_count"],
               "approved": False, "release_ready": False, "client_available": False,
               "index_write_attempted": False, "publication_attempted": False, "upload_attempted": False,
               "outcomes": []}
    index_key = f"clients/{manifest['client_id']}/release-index.json"
    try:
        before = storage.read_index(index_key)
        parser = _DraftLinks()
        read_page = getattr(storage, "read_private_draft_page", None)
        if callable(read_page):
            folder = safe_path(runtime_path(root, "data/outputs"), issue_id, "revision-" + str(revision))
            progress_path = folder / "private-owner-page-progress.json"
            binding = {"client_id": manifest["client_id"], "issue_id": issue_id, "revision": revision,
                       "package_hash": manifest["package_hash"], "index_etag": before["etag"], "index_hash": digest(before["index"])}
            try:
                saved = load_json(progress_path, 16 * 1024) if progress_path.exists() else None
            except (SafetyError, OSError, ValueError):
                saved = None  # Disposable pagination hint, not packet authority.
            valid_progress = (isinstance(saved, dict) and set(saved) == {"schema_version", "binding", "next_cursor"}
                and type(saved["schema_version"]) is int and saved["schema_version"] == 1 and saved["binding"] == binding
                and (saved["next_cursor"] is None or isinstance(saved["next_cursor"], str)
                    and re.fullmatch(r"[A-Za-z0-9_-]{1,3072}", saved["next_cursor"])))
            cursor = saved["next_cursor"] if valid_progress else None
            visited, found = set(), False
            for _ in range(max_pages):
                if cursor is not None and cursor in visited:
                    raise SafetyError("PRIVATE_DRAFT_PAGE_LOOP")
                visited.add(cursor)
                try:
                    page = read_page(cursor=cursor)
                except Exception:
                    # A changed storage listing invalidates its old continuation
                    # even when the release root is unchanged. The next pass
                    # starts afresh; it must not retry that stale cursor forever.
                    atomic_json(progress_path, {"schema_version": 1, "binding": binding, "next_cursor": None})
                    raise
                if (page.get("client_id") != binding["client_id"] or page.get("index_etag") != binding["index_etag"]
                        or page.get("index_hash") != binding["index_hash"]):
                    raise SafetyError("PRIVATE_DRAFT_INDEX_CHANGED")
                receipt["owner_pages_checked"] = receipt.get("owner_pages_checked", 0) + 1
                matching = [row for row in page["drafts"] if row["issue_id"] == issue_id and row["revision"] == revision]
                if matching:
                    row = matching[0]
                    expected_files = {item["name"]: (item["sha256"], item["size"]) for item in manifest["files"]}
                    listed_files = {item["name"]: (item["sha256"], item["size"]) for item in row["files"]}
                    if (len(matching) != 1 or row["package_hash"] != manifest["package_hash"]
                            or row["product_count"] != manifest["product_count"] or expected_files != listed_files):
                        raise SafetyError("PRIVATE_DRAFT_READBACK_MISMATCH")
                    parser.links.update(item["url"] for item in row["files"])
                    found = True
                    cursor = None
                    break
                cursor = page["next_cursor"]
                if cursor is None: break
            atomic_json(progress_path, {"schema_version": 1, "binding": binding, "next_cursor": cursor})
            if not found and cursor is not None:
                return dict(receipt, state="owner_page_search_continuation", owner_page_verified=False,
                            continuation_saved=True, reason="PRIVATE_DRAFT_PAGE_SEARCH_CONTINUES")
        else:
            parser.feed(storage.read_private_draft_list())
            parser.close()
    except Exception as error:
        return dict(receipt, reason=_reason(error), owner_page_verified=False)
    for item in files:
        if item["name"] not in ("shortlist.xlsx", "report-en.pdf", "report-hi-Latn.pdf"):
            continue
        path = f"/admin/download/{issue_id}/{revision}/{item['name']}"
        try:
            if path not in parser.links:
                raise SafetyError("PRIVATE_DRAFT_DOWNLOAD_LINK_MISSING")
            raw = storage.read_private_draft_download(item["key"], sha256=item["sha256"], size=item["size"])
            if raw != item["raw"]:
                raise SafetyError("PRIVATE_DRAFT_READBACK_MISMATCH")
            receipt["outcomes"].append({"name": item["name"], "state": "verified", "path": path,
                                         "size": item["size"], "sha256": item["sha256"]})
        except Exception as error:
            reason = "PRIVATE_DRAFT_DOWNLOAD_LINK_MISSING" if isinstance(error, SafetyError) and str(error) == "PRIVATE_DRAFT_DOWNLOAD_LINK_MISSING" else _reason(error)
            receipt["outcomes"].append({"name": item["name"], "state": "needs_follow_up", "reason": reason})
    receipt["owner_page_verified"] = True
    receipt["verified_download_count"] = sum(row["state"] == "verified" for row in receipt["outcomes"])
    try:
        after = storage.read_index(index_key)
        receipt["index_unchanged"] = before["etag"] == after["etag"] and before["index"] == after["index"]
        if receipt["index_unchanged"] and receipt["verified_download_count"] == len(manifest["files"]):
            receipt["state"] = "owner_downloads_verified"
        elif not receipt["index_unchanged"]:
            receipt.update(state="index_changed_elsewhere", reason="PRIVATE_DRAFT_INDEX_CHANGED")
    except Exception as error:
        receipt.update(state="index_check_waiting", reason=_reason(error), index_unchanged=None)
    return receipt
