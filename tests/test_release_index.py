"""Fake remote storage only; tests do not approve/upload real client files."""

from copy import deepcopy
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.delivery_package import build_delivery_manifest
from product_radar.live_outputs import digest_bytes, write_issue_package
from product_radar.models import SafetyError, canonical, digest
from product_radar.release_index import (activate_delivery_package, apply_release_mutation, plan_activation,
    plan_withdrawal, project_issue_ordinal, publication_receipt, release_receipt, scheduled_utc, validate_release_index, withdraw_release)
from test_delivery_package import packet
from product_radar.storage import load_json

SCHEDULE = "2026-10-13T02:30:00+00:00"
BEFORE = "2026-10-12T10:00:00+00:00"
AFTER = "2026-10-13T03:00:00+00:00"


class FakeStorage:
    def __init__(self, client="default", now=BEFORE):
        self.index = {"schema_version": 1, "client_id": client, "issues": []}
        self.now, self.etag, self.version = now, None, 0
        self.objects, self.calls = {}, []
        self.conflict = self.fail_write = self.corrupt_object = self.fail_after_commit = False

    def read_index(self, key):
        self.calls.append(("read_index", key))
        return {"index": deepcopy(self.index), "etag": self.etag, "server_observed_at": self.now, "verified": True, "authority": "release_index"}

    def put_immutable(self, key, raw, *, sha256, media_type):
        self.calls.append(("put", key))
        if key in self.objects and self.objects[key] != raw:
            raise SafetyError("FAKE_IMMUTABLE_OBJECT_CONFLICT")
        self.objects[key] = raw

    def read_object(self, key):
        return b"changed" if self.corrupt_object else self.objects[key]

    def compare_and_swap_index(self, key, expected_etag, mutation):
        self.calls.append(("cas", expected_etag))
        if self.conflict or expected_etag != self.etag:
            return None
        if self.fail_write:
            raise SafetyError("FAKE_WRITE_FAILED")
        updated = apply_release_mutation(self.index, mutation, server_at=self.now)
        if updated != self.index:
            self.index = updated
            self.version += 1
            self.etag = f"actual-opaque-etag-{self.version}"
        if self.fail_after_commit:
            raise SafetyError("FAKE_UNCERTAIN_RESPONSE")
        return self.read_index(key)


def approved(manifest, scheduled=SCHEDULE, **extra):
    return {"approved": True, "owner_id": "owner-one", **{field: manifest[field] for field in
            ("client_id", "issue_id", "revision", "selection_hash", "package_hash", "publication_ordinal")}, "scheduled_at": scheduled, **extra}


def prepared(root):
    issue, _, drafts, receipts = packet(root)
    manifest = build_delivery_manifest(issue, drafts, receipts, root=root)
    return issue, manifest


def revised(root, issue, manifest):
    changed = deepcopy(issue)
    changed["revision"] += 1
    changed["selection_hash"] = digest({field: changed[field] for field in ("issue_id", "client_id", "revision", "date", "products", "evidence_fingerprint")})
    dataset = load_json(manifest["artifacts"]["facts"]["path"], 4 * 1024 * 1024)
    dataset.update(revision=changed["revision"], selection_hash=changed["selection_hash"])
    drafts, receipts = {}, {}
    for language in manifest["languages"]:
        draft = write_issue_package(changed, dataset, root, language=language)
        raw = Path(manifest["pdf_receipts"][language]["path"]).read_bytes()
        path = Path(draft["artifacts"]["html"]["path"]).with_suffix(".pdf")
        path.write_bytes(raw)
        drafts[language] = draft
        receipts[language] = {"path": str(path), "pdf_sha256": digest_bytes(raw), "html_sha256": draft["artifacts"]["html"]["sha256"],
                              "pages": 1, "portrait": True, "bytes": len(raw)}
    return changed, build_delivery_manifest(changed, drafts, receipts, root=root)


def entry(issue_id, ordinal, schedule, *, revision=1):
    languages = ["en", "hi-Latn"] if ordinal <= 2 else ["en"]
    return {"issue_id": issue_id, "revision": revision, "title": "Product picks", "approved": True, "complete": True,
            "remote_state": "active", "scheduled_at": schedule, "server_activated_at": BEFORE,
            "product_count": 10, "new_count": 10, "package_hash": digest([issue_id, revision]), "selection_hash": "b" * 64,
            "publication_ordinal": ordinal, "languages": languages, "concept_ids": [issue_id + str(n) for n in range(10)],
            "files": [{"name": name, "key": f"clients/default/issues/{issue_id}/r{revision}/{name}", "size": 42, "sha256": "a" * 64,
                       "media_type": "application/pdf" if name.endswith("pdf") else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
                      for name in ["shortlist.xlsx", *("report-" + lang + ".pdf" for lang in languages)]]}


class ReleaseIndexTests(unittest.TestCase):
    def activate(self, storage, root, issue, manifest, **extra):
        return activate_delivery_package(storage, issue, manifest, root=root, approval=approved(manifest), scheduled_at=SCHEDULE,
                                         expected_etag=storage.etag, allow_synthetic=True, **extra)

    def test_configured_india_morning_converts_to_utc_without_default(self):
        self.assertEqual(scheduled_utc("2026-10-13", "08:00"), SCHEDULE)
        for day, clock in (("2026-10-13", None), ("2026-10-13", "8:00"), ("2026-10-12", "08:00")):
            with self.assertRaises(SafetyError):
                scheduled_utc(day, clock)

    def test_objects_verified_before_index_and_upload_is_not_publication(self):
        with tempfile.TemporaryDirectory() as root:
            issue, manifest = prepared(root)
            storage = FakeStorage()
            result = self.activate(storage, root, issue, manifest)
            self.assertFalse(result["published"])
            self.assertIsNone(publication_receipt(result["snapshot"], issue["issue_id"], 1))
            self.assertEqual(result["receipt"]["server_activated_at"], BEFORE)
            self.assertEqual(len(storage.objects), 5)  # Three downloads, private facts/manifest.
            self.assertTrue(storage.index["issues"][0]["approved"])
            self.assertFalse(manifest["approved"])
            methods = [call[0] for call in storage.calls]
            self.assertLess(max(i for i, name in enumerate(methods) if name == "put"), methods.index("cas"))

    def test_explicit_approval_is_bound_to_exact_package_schedule_and_owner(self):
        with tempfile.TemporaryDirectory() as root:
            issue, manifest = prepared(root)
            for updates in ({"approved": False}, {"package_hash": "a" * 64}, {"selection_hash": "c" * 64},
                            {"owner_id": None}, {"scheduled_at": "2026-10-15T02:30:00+00:00"}, {"revision": 2}):
                storage = FakeStorage()
                with self.assertRaises(SafetyError):
                    activate_delivery_package(storage, issue, manifest, root=root, approval=dict(approved(manifest), **updates),
                                              scheduled_at=SCHEDULE, expected_etag=None, allow_synthetic=True)
                self.assertEqual(storage.objects, {})

    def test_synthetic_is_refused_without_explicit_rehearsal_option(self):
        with tempfile.TemporaryDirectory() as root:
            issue, manifest = prepared(root)
            with self.assertRaisesRegex(SafetyError, "SYNTHETIC"):
                activate_delivery_package(FakeStorage(), issue, manifest, root=root, approval=approved(manifest),
                                          scheduled_at=SCHEDULE, expected_etag=None)

    def test_etag_conflict_or_write_failure_keeps_previous_index_and_orphans_private(self):
        for failure in ("conflict", "fail_write", "corrupt_object"):
            with tempfile.TemporaryDirectory() as root:
                issue, manifest = prepared(root)
                storage = FakeStorage()
                setattr(storage, failure, True)
                with self.assertRaises(SafetyError):
                    self.activate(storage, root, issue, manifest)
                self.assertEqual(storage.index["issues"], [])
                self.assertIsNone(storage.etag)
                self.assertTrue(storage.objects)  # Uploaded, but not indexed/servable.

    def test_stale_actual_etag_rejected_before_uploads(self):
        with tempfile.TemporaryDirectory() as root:
            issue, manifest = prepared(root)
            storage = FakeStorage()
            storage.etag = "actual-other-version"
            with self.assertRaisesRegex(SafetyError, "INDEX_CONFLICT"):
                activate_delivery_package(storage, issue, manifest, root=root, approval=approved(manifest),
                                          scheduled_at=SCHEDULE, expected_etag="old-version", allow_synthetic=True)
            self.assertEqual(storage.objects, {})

    def test_future_report_becomes_published_by_server_observation_not_download(self):
        with tempfile.TemporaryDirectory() as root:
            issue, manifest = prepared(root)
            storage = FakeStorage()
            self.activate(storage, root, issue, manifest)
            storage.now = AFTER
            receipt = publication_receipt(storage.read_index("index"), issue["issue_id"], 1)
            self.assertTrue(receipt["published"])
            self.assertEqual(receipt["available_at"], SCHEDULE)
            self.assertEqual(receipt["authority"], "release_index")

    def test_never_released_withdrawal_has_no_history_but_confirmed_state(self):
        with tempfile.TemporaryDirectory() as root:
            issue, manifest = prepared(root)
            storage = FakeStorage()
            self.activate(storage, root, issue, manifest)
            result = withdraw_release(storage, "default", issue["issue_id"], 1, manifest["package_hash"], expected_etag=storage.etag,
                approval={"approved": True, "owner_id": "owner-one", "client_id": "default", "issue_id": issue["issue_id"], "revision": 1,
                          "package_hash": manifest["package_hash"], "action": "withdraw"})
            self.assertFalse(result["published"])
            self.assertEqual(result["receipt"]["remote_state"], "withdrawn")
            self.assertIsNone(publication_receipt(result["snapshot"], issue["issue_id"], 1))

    def test_published_then_withdrawn_retains_nonempty_available_interval(self):
        storage = FakeStorage(now=AFTER)
        row = entry("published-first", 1, SCHEDULE)
        storage.index["issues"] = [row]
        storage.etag = "actual-old"
        result = withdraw_release(storage, "default", row["issue_id"], 1, row["package_hash"], expected_etag=storage.etag,
                                  approval={"approved": True, "owner_id": "owner-one", "client_id": "default", "issue_id": row["issue_id"],
                                            "revision": 1, "package_hash": row["package_hash"], "action": "withdraw"})
        self.assertTrue(result["published"])
        self.assertEqual(result["receipt"]["available_at"], SCHEDULE)
        self.assertEqual(result["receipt"]["server_deactivated_at"], AFTER)
        self.assertEqual(project_issue_ordinal(storage.index, "next-issue", "2026-10-15T02:30:00+00:00", server_at=AFTER), 2)

    def test_future_withdrawal_stamp_is_not_a_verified_receipt(self):
        storage = FakeStorage()
        row = entry("future", 1, SCHEDULE)
        row.update(remote_state="withdrawn", server_deactivated_at=AFTER)
        storage.index["issues"] = [row]
        storage.etag = "actual-old"
        with self.assertRaisesRegex(SafetyError, "SERVER_TIME_INVALID"):
            release_receipt(storage.read_index("index"), "future", 1)

    def test_distinct_issue_ordinals_not_revision_count(self):
        index = {"schema_version": 1, "client_id": "default", "issues": [entry("first", 1, SCHEDULE), entry("first", 1, SCHEDULE, revision=2)]}
        index["issues"][0].update(remote_state="withdrawn", server_deactivated_at=AFTER)
        index["issues"][1]["server_activated_at"] = AFTER
        self.assertEqual(project_issue_ordinal(index, "second", "2026-10-15T02:30:00+00:00", server_at=AFTER), 2)
        self.assertEqual(project_issue_ordinal(index, "first", SCHEDULE, server_at=AFTER), 1)

    def test_queue_withdrawal_atomically_defers_reprojected_unreleased_languages(self):
        storage = FakeStorage()
        first, second, third = entry("first", 1, SCHEDULE), entry("second", 2, "2026-10-15T02:30:00+00:00"), entry("third", 3, "2026-10-17T02:30:00+00:00")
        storage.index["issues"] = [first, second, third]
        storage.etag = "actual-old"
        approval = {"approved": True, "owner_id": "owner-one", "client_id": "default", "issue_id": "first", "revision": 1, "package_hash": first["package_hash"], "action": "withdraw"}
        result = withdraw_release(storage, "default", "first", 1, first["package_hash"], approval=approval, expected_etag=storage.etag)
        self.assertEqual([e["remote_state"] for e in result["snapshot"]["index"]["issues"]], ["withdrawn"] * 3)
        self.assertEqual(second["remote_state"], "active")  # Caller input untouched.
        self.assertEqual(storage.index["issues"][2]["withdrawal_reason"], "queue_reprojection_required")
        self.assertEqual(result["deferred_issue_ids"], ["second", "third"])
        self.assertIsNone(publication_receipt(storage.read_index("index"), "third", 1))

    def test_wrong_previous_revision_does_not_upload_replacement(self):
        with tempfile.TemporaryDirectory() as root:
            issue, manifest = prepared(root)
            old = entry(issue["issue_id"], 1, SCHEDULE)
            storage = FakeStorage()
            storage.index["issues"] = [old]
            storage.etag = "actual-old"
            with self.assertRaises(SafetyError):
                self.activate(storage, root, issue, manifest, expected_previous={"revision": 1, "package_hash": "f" * 64})
            self.assertEqual(storage.objects, {})

    def test_uncertain_commit_recovers_same_package_without_second_activation(self):
        with tempfile.TemporaryDirectory() as root:
            issue, manifest = prepared(root)
            storage = FakeStorage()
            storage.fail_after_commit = True
            with self.assertRaises(SafetyError):
                self.activate(storage, root, issue, manifest)
            activation = storage.index["issues"][0]["server_activated_at"]
            storage.fail_after_commit = False
            result = self.activate(storage, root, issue, manifest)
            self.assertEqual(len(storage.index["issues"]), 1)
            self.assertEqual(storage.index["issues"][0]["server_activated_at"], activation)
            self.assertEqual(storage.version, 1)
            self.assertFalse(result["published"])

    def test_late_approval_requires_explicit_release_consequence(self):
        with tempfile.TemporaryDirectory() as root:
            issue, manifest = prepared(root)
            storage = FakeStorage(now=AFTER)
            with self.assertRaisesRegex(SafetyError, "LATE_CONSEQUENCE"):
                self.activate(storage, root, issue, manifest)
            result = activate_delivery_package(storage, issue, manifest, root=root, approval=approved(manifest, allow_late=True),
                                              scheduled_at=SCHEDULE, expected_etag=None, allow_synthetic=True)
            self.assertTrue(result["published"])
            self.assertEqual(result["receipt"]["available_at"], AFTER)

    def test_index_count_novelty_files_and_trust_are_checked(self):
        row = entry("first", 1, SCHEDULE)
        for changes in ({"product_count": 9}, {"new_count": 7}, {"languages": ["en"]}, {"approved": 1}):
            with self.assertRaises(SafetyError):
                validate_release_index({"schema_version": 1, "client_id": "default", "issues": [dict(row, **changes)]}, "default", server_at=BEFORE)
        storage = FakeStorage()
        snapshot = storage.read_index("index")
        snapshot["verified"] = False
        with self.assertRaises(SafetyError):
            publication_receipt(snapshot, "first", 1)

    def test_generated_index_matches_actual_portal_validator(self):
        with tempfile.TemporaryDirectory() as root:
            issue, manifest = prepared(root)
            storage = FakeStorage()
            result = self.activate(storage, root, issue, manifest)
            script = "import {validateIndex} from './portal/worker.mjs'; let raw=''; for await (const part of process.stdin) raw+=part; validateIndex(JSON.parse(raw),'default');"
            check = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT,
                                   input=canonical(result["snapshot"]["index"]), capture_output=True, timeout=20)
            self.assertEqual(check.returncode, 0, check.stderr.decode())

    def test_other_queued_concept_hold_blocks_upload_without_research_repeat(self):
        with tempfile.TemporaryDirectory() as root:
            issue, manifest = prepared(root)
            storage = FakeStorage()
            other = entry("later-other", 1, "2026-10-15T02:30:00+00:00")
            other["concept_ids"] = manifest["concept_ids"]
            storage.index["issues"] = [other]
            storage.etag = "actual-prior"
            with self.assertRaisesRegex(SafetyError, "DUPLICATE_OR_QUEUED"):
                self.activate(storage, root, issue, manifest)
            self.assertEqual(storage.objects, {})

    def test_same_issue_correction_supersedes_atomically_and_keeps_old_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            issue, manifest = prepared(root)
            storage = FakeStorage(now=AFTER)
            old = entry(issue["issue_id"], 1, SCHEDULE)
            storage.index["issues"] = [old]
            storage.etag = "old-opaque-version"
            # Pure mutation fixture: hashes recomputed, no real files uploaded.
            replacement = deepcopy(manifest)
            replacement["revision"] = 2
            for file in replacement["files"]:
                file["key"] = file["key"].replace("/r1/", "/r2/")
            replacement["package_hash"] = digest({k: v for k, v in replacement.items() if k != "package_hash"})
            mutation = plan_activation(storage.index, replacement, approved(replacement, allow_late=True),
                scheduled_at=SCHEDULE, server_at=AFTER, allow_synthetic=True,
                expected_previous={"revision": 1, "package_hash": old["package_hash"]})
            updated = apply_release_mutation(storage.index, mutation, server_at=AFTER)
            storage.index, storage.etag = updated, "new-version"
            self.assertEqual([row["remote_state"] for row in updated["issues"]], ["withdrawn", "active"])
            self.assertTrue(publication_receipt(storage.read_index("index"), issue["issue_id"], 1)["published"])
            self.assertEqual(project_issue_ordinal(updated, "next", "2026-10-15T02:30:00+00:00", server_at=AFTER), 2)

    def test_failed_real_packet_replacement_keeps_old_active_and_new_orphans_unselected(self):
        with tempfile.TemporaryDirectory() as root:
            issue, manifest = prepared(root)
            storage = FakeStorage()
            self.activate(storage, root, issue, manifest)
            old_index, old_etag = deepcopy(storage.index), storage.etag
            changed, replacement = revised(root, issue, manifest)
            storage.fail_write = True
            with self.assertRaisesRegex(SafetyError, "FAKE_WRITE_FAILED"):
                self.activate(storage, root, changed, replacement,
                              expected_previous={"revision": 1, "package_hash": manifest["package_hash"]})
            self.assertEqual(storage.index, old_index)
            self.assertEqual(storage.etag, old_etag)
            self.assertEqual(len(storage.objects), 10)
            self.assertEqual(storage.index["issues"][0]["revision"], 1)

    def test_missing_trusted_server_time_cannot_be_substituted_with_laptop_time(self):
        storage = FakeStorage()
        snapshot = storage.read_index("index")
        snapshot.pop("server_observed_at")
        with self.assertRaises(SafetyError):
            release_receipt(snapshot, "first", 1)

    def test_future_replacement_does_not_silently_hide_already_available_issue(self):
        with tempfile.TemporaryDirectory() as root:
            issue, manifest = prepared(root)
            old = entry(issue["issue_id"], 1, SCHEDULE)
            index = {"schema_version": 1, "client_id": "default", "issues": [old]}
            changed = deepcopy(manifest)
            changed.update(revision=2, release_date="2026-10-15")
            for file in changed["files"]:
                file["key"] = file["key"].replace("/r1/", "/r2/")
            changed["package_hash"] = digest({k: v for k, v in changed.items() if k != "package_hash"})
            future = "2026-10-15T02:30:00+00:00"
            kwargs = {"scheduled_at": future, "server_at": AFTER, "allow_synthetic": True,
                      "expected_previous": {"revision": 1, "package_hash": old["package_hash"]}}
            with self.assertRaisesRegex(SafetyError, "VISIBILITY_REVIEW_REQUIRED"):
                plan_activation(index, changed, approved(changed, future), **kwargs)
            mutation = plan_activation(index, changed, approved(changed, future, allow_visibility_gap=True), **kwargs)
            self.assertEqual(apply_release_mutation(index, mutation, server_at=AFTER)["issues"][0]["remote_state"], "withdrawn")


if __name__ == "__main__":
    unittest.main()
