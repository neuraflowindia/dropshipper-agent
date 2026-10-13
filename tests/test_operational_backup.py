"""Offline exact-record backup/recovery; no provider, model or billing calls."""

from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar.models import SafetyError, digest
from product_radar.operational_backup import (create_operational_backup, restore_operational_backup,
                                              verify_operational_backup)
from product_radar.research_memory import ResearchMemory, change
from product_radar.storage import Journal, atomic_bytes, atomic_json, load_json


class OperationalBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.memory = ResearchMemory(self.root / "working/research", Journal(self.root / "working/finance"),
                                     evidence_type="synthetic")
        self.memory.put("concepts", "product-one", {"description": {"label": "Fictional draining rack"}})
        self.memory.put("published_history", "first-published", {"concept_id": "product-one", "status": "published", "issue_id": "first-issue"})
        with self.memory.journal.locked():
            self.memory.journal.setup_budget("synthetic-account:synthetic-period", "5")
        self.attachment = self.root / "public/multiTimeline.csv"
        atomic_bytes(self.attachment, b"Week,Fictional draining rack\n2026-01-04,10\n")

    def backup(self, **options):
        return create_operational_backup(self.memory, self.root / "backups", backup_id="rehearsal-one", **options)

    def manifest(self, result):
        return Path(result["folder"]) / "manifest.json"

    def change_manifest(self, result, mutate):
        manifest = load_json(self.manifest(result), 32 * 1024 * 1024)
        mutate(manifest)
        manifest["hash"] = digest({key: value for key, value in manifest.items() if key != "hash"})
        atomic_json(self.manifest(result), manifest)

    def test_restore_exact_events_finances_and_selected_original_bytes(self):
        research_events = {p.name: p.read_bytes() for p in (self.memory.root / "events").glob("*.json")}
        financial_bytes = self.memory.journal.path.read_bytes()
        result = self.backup(attachments=[{"path": self.attachment, "kind": "source_capture"}])
        receipt = restore_operational_backup(result["folder"], self.root / "restored")
        restored = ResearchMemory(self.root / "restored/research", Journal(self.root / "restored/finance"), evidence_type="synthetic")
        self.assertEqual(restored.snapshot(), self.memory.snapshot())
        self.assertEqual({p.name: p.read_bytes() for p in (restored.root / "events").glob("*.json")}, research_events)
        self.assertEqual(restored.journal.path.read_bytes(), financial_bytes)
        self.assertEqual(Path(receipt["attachment_mapping"][0]["restored_path"]).read_bytes(), self.attachment.read_bytes())
        self.assertEqual(receipt["research_events"], 2)
        with self.memory.journal.locked():
            financial_events = len(self.memory.journal.events())
        self.assertEqual(receipt["financial_events"], financial_events)
        self.assertEqual(receipt["provider_calls"], 0)
        self.assertEqual(receipt["model_calls"], 0)
        self.assertFalse(receipt["independent_backup_proved"])
        self.assertIn("automatic_reference_path_relocation", receipt["not_verified"])

    def test_existing_backup_reuses_its_original_checkpoint_not_later_state(self):
        first = self.backup()
        self.memory.put("concepts", "later-product", {"label": "Not in the earlier backup."})
        with patch("product_radar.operational_backup._checkpoint", side_effect=AssertionError("Must not take a new checkpoint")):
            again = self.backup()
        self.assertTrue(again["replayed"])
        self.assertEqual(again["manifest"], first["manifest"])
        self.assertEqual(again["manifest"]["research_checkpoint"]["table_counts"]["concepts"], 1)

    def test_disposable_index_and_unselected_files_are_not_backed_up(self):
        atomic_json(self.memory.root / "index.json", {"not": "canonical data"})
        atomic_json(self.memory.root / "unused-output.json", {"not": "selected"})
        result = self.backup()
        paths = {row["path"] for row in result["manifest"]["files"]}
        self.assertEqual(paths, {"research/events/00000001.json", "research/events/00000002.json", "finance/events.jsonl"})
        self.assertEqual(result["manifest"]["attachments"], [])

    def test_verification_does_not_change_backup_bytes_or_create_a_lock(self):
        result = self.backup()
        folder = Path(result["folder"])
        original = {str(path.relative_to(folder)): path.read_bytes() for path in folder.rglob("*") if path.is_file()}
        verify_operational_backup(folder)
        current = {str(path.relative_to(folder)): path.read_bytes() for path in folder.rglob("*") if path.is_file()}
        self.assertEqual(current, original)
        self.assertFalse((folder / "finance/runner.lock").exists())

    def test_existing_id_cannot_silently_claim_a_different_attachment_scope(self):
        result = self.backup(attachments=[{"path": self.attachment, "kind": "source_capture"}])
        self.assertTrue(self.backup(attachments=[{"path": self.attachment, "kind": "source_capture"}])["replayed"])
        with self.assertRaisesRegex(SafetyError, "ID_SCOPE_CONFLICT"):
            self.backup(attachments=[{"path": self.attachment, "kind": "model_receipt"}])
        self.assertEqual(verify_operational_backup(result["folder"]), result["manifest"])

    def test_original_canonical_corruption_does_not_create_a_ready_backup(self):
        event = self.memory.root / "events/00000001.json"
        raw = load_json(event)
        raw["changes"][0]["value"]["description"]["label"] = "Corrupted fixture"
        atomic_json(event, raw)
        with self.assertRaises(SafetyError): self.backup()
        self.assertFalse((self.root / "backups/rehearsal-one/manifest.json").exists())

    def test_corrupt_copied_bytes_are_rejected_before_any_restore_write(self):
        result = self.backup()
        atomic_bytes(Path(result["folder"]) / "research/events/00000001.json", b"corrupt")
        with self.assertRaisesRegex(SafetyError, "HASH_MISMATCH"):
            restore_operational_backup(result["folder"], self.root / "restored")
        self.assertFalse((self.root / "restored").exists())

    def test_resealed_wrong_chain_or_state_is_not_trusted(self):
        for field in ("head", "state_hash"):
            with self.subTest(field=field), tempfile.TemporaryDirectory(dir=self.root) as directory:
                result = create_operational_backup(self.memory, directory, backup_id="one")
                self.change_manifest(result, lambda value: value["research_checkpoint"].update({field: "f" * 64}))
                with self.assertRaisesRegex(SafetyError, "CHECKPOINT_MISMATCH"):
                    verify_operational_backup(result["folder"])

    def test_resealed_wrong_financial_checkpoint_is_not_trusted(self):
        result = self.backup()
        self.change_manifest(result, lambda value: value["finance_checkpoint"].update(head="f" * 64))
        with self.assertRaisesRegex(SafetyError, "FINANCIAL_CHECKPOINT_MISMATCH"):
            verify_operational_backup(result["folder"])

    def test_target_with_any_existing_file_is_not_overwritten(self):
        result = self.backup()
        existing = self.root / "restored/owner-file.txt"
        atomic_bytes(existing, b"Owner file must remain.")
        with self.assertRaisesRegex(SafetyError, "DESTINATION_NOT_EMPTY"):
            restore_operational_backup(result["folder"], existing.parent)
        self.assertEqual(existing.read_bytes(), b"Owner file must remain.")

    def test_destinations_inside_original_source_or_backup_are_rejected(self):
        for destination in (self.memory.root, self.memory.root / "backups", self.memory.journal.root / "backups"):
            with self.assertRaisesRegex(SafetyError, "DESTINATION_INSIDE_SOURCE"):
                create_operational_backup(self.memory, destination, backup_id="one")
        result = self.backup()
        for destination in (Path(result["folder"]), Path(result["folder"]) / "restore", self.root / "backups"):
            with self.assertRaisesRegex(SafetyError, "DESTINATION_NOT_EMPTY"):
                restore_operational_backup(result["folder"], destination)

    def test_path_traversal_and_missing_declared_scope_are_rejected(self):
        for path in ("../outside.txt", "/outside.txt", "attachments/../../outside.txt", "C:\\outside.txt", "config/auth.json"):
            with self.subTest(path=path), tempfile.TemporaryDirectory(dir=self.root) as directory:
                result = create_operational_backup(self.memory, directory, backup_id="one")
                self.change_manifest(result, lambda value: value["files"][0].update(path=path))
                with self.assertRaises(SafetyError): verify_operational_backup(result["folder"])

    def test_private_or_automatic_directory_copy_is_refused(self):
        for path in (self.root / "config/public.json", self.root / "public/.env", self.root / "public/auth.json",
                     self.root / ".codex/public.json", self.root / "public"):
            with self.subTest(path=path):
                with self.assertRaises(SafetyError):
                    self.backup(attachments=[{"path": path, "kind": "source_capture"}])
        with self.assertRaises(SafetyError): self.backup(attachments=[{"path": self.attachment, "kind": "credentials"}])

    def test_symlink_source_attachment_and_restoration_are_rejected(self):
        link = self.root / "public/link.csv"; link.symlink_to(self.attachment)
        with self.assertRaisesRegex(SafetyError, "SYMLINK"):
            self.backup(attachments=[{"path": link, "kind": "source_capture"}])
        result = self.backup()
        restore_link = self.root / "restore-link"; restore_link.symlink_to(self.root / "working")
        with self.assertRaisesRegex(SafetyError, "SYMLINK"):
            restore_operational_backup(result["folder"], restore_link)

    def test_attachment_duplicate_and_relabelled_mapping_are_rejected(self):
        item = {"path": self.attachment, "kind": "source_capture"}
        with self.assertRaisesRegex(SafetyError, "DUPLICATED"): self.backup(attachments=[item, item])
        result = self.backup(attachments=[item])
        self.change_manifest(result, lambda value: value["attachments"][0].update(original_path="not-the-captured-file.csv"))
        with self.assertRaisesRegex(SafetyError, "ATTACHMENT_INVALID"):
            verify_operational_backup(result["folder"])

    def test_corrupt_manifest_hash_is_rejected(self):
        result = self.backup(); manifest = deepcopy(result["manifest"])
        manifest["research_checkpoint"]["sequence"] = 0
        atomic_json(self.manifest(result), manifest)
        with self.assertRaisesRegex(SafetyError, "MANIFEST_INVALID"):
            verify_operational_backup(result["folder"])

    def test_failed_extra_file_keeps_working_records_and_has_no_ready_manifest(self):
        original = {p: p.read_bytes() for p in (self.memory.root / "events").glob("*.json")}
        with self.assertRaisesRegex(SafetyError, "FILE_UNAVAILABLE"):
            self.backup(attachments=[{"path": self.root / "public/missing.csv", "kind": "source_capture"}])
        self.assertTrue(all(path.read_bytes() == raw for path, raw in original.items()))
        self.assertFalse((self.root / "backups/rehearsal-one/manifest.json").exists())
        with self.assertRaises(SafetyError): self.backup()
        healthy = create_operational_backup(self.memory, self.root / "backups", backup_id="rehearsal-two")
        self.assertEqual(healthy["manifest"]["research_checkpoint"]["sequence"], 2)

    def test_empty_research_and_financial_namespaces_restore_without_invented_events(self):
        empty = ResearchMemory(self.root / "empty/research", Journal(self.root / "empty/finance"), evidence_type="synthetic")
        result = create_operational_backup(empty, self.root / "backups", backup_id="empty")
        receipt = restore_operational_backup(result["folder"], self.root / "restored-empty")
        self.assertEqual(receipt["research_events"], 0)
        self.assertEqual(receipt["financial_events"], 0)
        self.assertEqual(receipt["files_verified"], 1)


if __name__ == "__main__":
    unittest.main()
