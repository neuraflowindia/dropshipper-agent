"""Essential local second-copy proofs from disposable offline records only."""

from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar.models import SafetyError, digest
from product_radar.operational_backup import (ESSENTIAL_BACKUP_VERSION, create_essential_operational_backup,
    create_operational_backup, restore_operational_backup, verify_operational_backup)
from product_radar.operational_backup_selection import select_essential_backup_files
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, atomic_bytes, atomic_json, load_json


class EssentialOperationalBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "project"
        self.memory = ResearchMemory(self.root / "data/research/captured",
            Journal(self.root / "data/benchmarks/state/apify"), evidence_type="synthetic")
        self.memory.put("concepts", "fictional-product", {"description": {"label": "Fictional sink caddy"}})
        with self.memory.journal.locked():
            self.memory.journal.setup_budget("fictional-account:fictional-period", "5")
        self.code = self.root / "src/product_radar/fictional_module.py"
        atomic_bytes(self.code, b"# Fictional offline application fixture.\nVALUE = 1\n")
        self.settings = self.root / "config/operations/backup.json"
        atomic_json(self.settings, {"schema_version": 1, "enabled": True,
            "destination": "data/backups", "timezone": "Asia/Kolkata"})
        self.destination = self.root / "data/backups"

    def create(self, backup_id="essential-one", **kwargs):
        return create_essential_operational_backup(self.memory, self.destination,
            backup_id=backup_id, root=self.root, **kwargs)

    def source(self):
        folder = self.memory.root / "direct_marketplace/fixture-acquisition"
        html = folder / "rendered.html"; atomic_bytes(html, b"<p>Fictional public product</p>\n")
        receipt = {"state": "usable", "artifacts": {"rendered.html": sha256(html.read_bytes()).hexdigest()}}
        path = folder / "receipt.json"; atomic_json(path, receipt)
        self.memory.put("captures", "source-capture", {"source": "amazon", "reference": str(path),
            "capture_hash": digest(receipt)})
        return path, html

    def reseal(self, folder, change):
        path = Path(folder) / "manifest.json"; value = load_json(path, 32 * 1024 * 1024)
        change(value)
        value["hash"] = digest({name: row for name, row in value.items() if name != "hash"})
        atomic_json(path, value)

    def test_exact_local_code_public_settings_source_bytes_restore_with_no_relocation_claim(self):
        source, html = self.source()
        original_state = self.memory.snapshot(); financial_bytes = self.memory.journal.path.read_bytes()
        originals = {path: path.read_bytes() for path in (self.code, self.settings, source, html)}
        with patch("urllib.request.urlopen", side_effect=AssertionError("No provider")), \
                patch("product_radar.research_ai.CodexRunner.__call__", side_effect=AssertionError("No model")):
            result = self.create()
            receipt = restore_operational_backup(result["folder"], self.root.parent / "isolated-restore")
        manifest = result["manifest"]
        self.assertEqual(manifest["kind"], ESSENTIAL_BACKUP_VERSION)
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(result["state"], "essential_files_verified")
        self.assertTrue(result["essential_files_complete"])
        self.assertFalse(result["operational_recovery_proved"])
        self.assertFalse(result["independent_backup_proved"])
        mappings = {row["original_path"]: row for row in receipt["attachment_mapping"]}
        for path, raw in originals.items():
            self.assertEqual(Path(mappings[str(path)]["restored_path"]).read_bytes(), raw)
            self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(self.memory.snapshot(), original_state)
        self.assertEqual(self.memory.journal.path.read_bytes(), financial_bytes)
        self.assertEqual(receipt["coverage_state"], "essential_files_verified")
        self.assertFalse(receipt["operational_recovery_proved"])
        self.assertIn("automatic_reference_path_relocation", receipt["not_verified"])
        self.assertIn("automatic_application_resumption", receipt["not_verified"])

    def test_one_verified_research_prefix_and_finance_checkpoint_feed_selection(self):
        calls = []; state_method = self.memory._state; journal_state = self.memory.journal.state
        def state(**kwargs):
            calls.append("research"); return state_method(**kwargs)
        def finance():
            calls.append("finance"); return journal_state()
        seen = []
        def selecting(state, finances, **kwargs):
            seen.append((digest(state), digest(finances)))
            return select_essential_backup_files(state, finances, **kwargs)
        with patch.object(self.memory, "_state", side_effect=state), \
                patch.object(self.memory.journal, "state", side_effect=finance), \
                patch("product_radar.operational_backup_selection.select_essential_backup_files", side_effect=selecting):
            result = self.create()
        self.assertEqual(calls, ["research", "finance"])
        self.assertEqual(seen, [(result["manifest"]["research_checkpoint"]["state_hash"],
            result["manifest"]["finance_checkpoint"]["state_hash"])])

    def test_every_typed_essential_group_is_copied_once_and_restore_maps_original_bytes(self):
        from product_radar.operational_backup_selection import ESSENTIAL_FILE_KINDS
        self.source()
        photo = self.root / "media/live/product-images/fictional-product.png"; atomic_bytes(photo, b"Fictional photo bytes")
        self.memory.put("source_checks", "photo-proof", {"kind": "public_product_image", "concept_id": "fictional-product",
            "image": {"path": str(photo), "sha256": sha256(photo.read_bytes()).hexdigest()}})
        request = {"role": "E", "model": "fictional", "payload": {"items": []}}
        self.memory.put("jobs", "fictional-job", {"request": request, "binding_keys": {}})
        job = self.memory.root / "ai_jobs/fictional-job"
        atomic_json(job / "intent.json", {"request": request})
        atomic_json(job / "result.json", {"request_hash": digest(request), "response": {}, "usage": None})
        material = self.memory.root / "catalogue_materials/items/fictional/material.json"
        atomic_json(material, {"schema_version": 1, "examples": []})
        self.memory.put("batch_runs", "catalogue-context", {"kind": "fictional-material-context", "reference": str(material)})
        control = self.memory.root / "programme_service/control.json"
        public_control = {"schema_version": 1, "enabled": True, "at": "2026-10-08T12:00:00+00:00"}
        atomic_json(control, {"control": public_control, "hash": digest(public_control)})
        issue = self.root / "data/outputs/fictional-issue/revision-1"
        atomic_json(issue / "package-input.json", {"products": [{"image_provenance": {
            "path": str(photo), "sha256": sha256(photo.read_bytes()).hexdigest()}}]})
        atomic_json(issue / "delivery-manifest.json", {"schema_version": 1})
        atomic_bytes(issue / "shortlist.xlsx", b"Fictional workbook bytes")
        self.memory.put("issue_reservations", "fictional-reservation", {
            "issue_id": "fictional-issue", "revision": 1, "state": "reserved"})
        atomic_bytes(self.root / "media/NeuraFlow/long_maroon_path_transparent_1024.png", b"Fictional public brand asset")
        atomic_bytes(self.root / "requirements-research.txt", b"fictional-dependency==1\n")
        atomic_bytes(self.root / "docs/README.md", b"Fictional operator instructions.\n")
        result = self.create(pending_controls=[control])
        kinds = {row["kind"] for row in result["manifest"]["attachments"]}
        self.assertEqual(kinds, set(ESSENTIAL_FILE_KINDS))
        paths = [row["original_path"] for row in result["manifest"]["attachments"]]
        self.assertEqual(paths.count(str(photo)), 1)
        proof = restore_operational_backup(result["folder"], self.root.parent / "all-kinds-restore")
        for row in proof["attachment_mapping"]:
            self.assertEqual(Path(row["restored_path"]).read_bytes(), Path(row["original_path"]).read_bytes())

    def test_new_event_during_selection_does_not_enlarge_original_prefix(self):
        def selecting(state, finances, **kwargs):
            self.memory.put("concepts", "later-product", {"description": {"label": "Outside saved prefix"}})
            return select_essential_backup_files(state, finances, **kwargs)
        with patch("product_radar.operational_backup_selection.select_essential_backup_files", side_effect=selecting):
            result = self.create()
        self.assertEqual(result["manifest"]["research_checkpoint"]["sequence"], 1)
        self.assertEqual(result["manifest"]["research_checkpoint"]["table_counts"]["concepts"], 1)
        self.assertFalse(any(row["path"] == "research/events/00000002.json" for row in result["manifest"]["files"]))
        self.assertEqual(len(self.memory.snapshot()["concepts"]), 2)

    def test_same_id_replays_original_without_new_checkpoint_or_selection(self):
        original = self.create()
        self.memory.put("concepts", "later-product", {"description": {"label": "Later"}})
        with patch("product_radar.operational_backup._checkpoint", side_effect=AssertionError("No new checkpoint")), \
                patch("product_radar.operational_backup_selection.select_essential_backup_files", side_effect=AssertionError("No reselect")):
            replay = self.create()
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["manifest"], original["manifest"])

    def test_old_v1_same_id_cannot_be_relabelled_essential(self):
        first = create_operational_backup(self.memory, self.destination, backup_id="essential-one")
        raw = (Path(first["folder"]) / "manifest.json").read_bytes()
        with self.assertRaisesRegex(SafetyError, "ID_SCOPE_CONFLICT"):
            self.create()
        self.assertEqual((Path(first["folder"]) / "manifest.json").read_bytes(), raw)

    def test_v1_creation_keeps_old_privacy_policy_and_cannot_adopt_v2_id(self):
        self.create()
        with self.assertRaisesRegex(SafetyError, "ID_SCOPE_CONFLICT"):
            create_operational_backup(self.memory, self.destination, backup_id="essential-one")
        with self.assertRaisesRegex(SafetyError, "ATTACHMENT_INVALID"):
            create_operational_backup(self.memory, self.destination, backup_id="legacy-code",
                attachments=[{"path": self.code, "kind": "application_code"}])
        with self.assertRaisesRegex(SafetyError, "PRIVATE_FILE_REFUSED"):
            create_operational_backup(self.memory, self.destination, backup_id="legacy-setting",
                attachments=[{"path": self.settings, "kind": "source_capture"}])

    def test_different_project_root_cannot_replay_original_essential_identity(self):
        first = self.create()
        with self.assertRaisesRegex(SafetyError, "ESSENTIAL_PROJECT_BINDING_INVALID"):
            create_essential_operational_backup(self.memory, self.destination, backup_id="essential-one",
                root=self.root.parent / "foreign-project")
        self.assertEqual(verify_operational_backup(first["folder"]), first["manifest"])

    def test_foreign_research_or_finance_namespace_is_rejected_before_checkpoint_selection_or_replay(self):
        original = self.create()
        foreign = self.root.parent / "foreign-project"
        choices = {
            "research": ResearchMemory(foreign / "data/research/captured", self.memory.journal, evidence_type="synthetic"),
            "finance": ResearchMemory(self.memory.root,
                Journal(foreign / "data/benchmarks/state/apify"), evidence_type="synthetic"),
        }
        for namespace, memory in choices.items():
            for backup_id in ("essential-one", "foreign-" + namespace):
                with self.subTest(namespace=namespace, backup_id=backup_id), \
                        patch("product_radar.operational_backup._checkpoint", side_effect=AssertionError("No foreign checkpoint")), \
                        patch("product_radar.operational_backup_selection.select_essential_backup_files", side_effect=AssertionError("No foreign selection")):
                    with self.assertRaisesRegex(SafetyError, "ESSENTIAL_PROJECT_BINDING_INVALID"):
                        create_essential_operational_backup(memory, self.destination,
                            backup_id=backup_id, root=self.root)
                self.assertFalse((self.destination / ("foreign-" + namespace)).exists())
        self.assertEqual(verify_operational_backup(original["folder"]), original["manifest"])

    def test_resealed_foreign_canonical_original_paths_fail_metadata_validation_without_source_reads(self):
        for relative in ("research/events/00000001.json", "finance/events.jsonl"):
            with self.subTest(relative=relative):
                result = self.create(backup_id="foreign-core-" + relative.split("/")[0])
                def corrupt(manifest):
                    row = next(row for row in manifest["files"] if row["path"] == relative)
                    row["original_path"] = str(self.root.parent / "foreign-project" / relative)
                self.reseal(result["folder"], corrupt)
                with patch("product_radar.operational_backup._read", side_effect=AssertionError("No original or payload read")):
                    with self.assertRaisesRegex(SafetyError, "ESSENTIAL_PROJECT_BINDING_INVALID"):
                        verify_operational_backup(result["folder"])
                destination = self.root.parent / ("foreign-core-restore-" + relative.split("/")[0])
                with self.assertRaisesRegex(SafetyError, "ESSENTIAL_PROJECT_BINDING_INVALID"):
                    restore_operational_backup(result["folder"], destination)
                self.assertFalse(destination.exists())

    def test_required_missing_reference_is_limited_not_an_operational_recovery_claim(self):
        missing = self.memory.root / "direct_marketplace/absent/receipt.json"
        self.memory.put("captures", "missing", {"source": "amazon", "reference": str(missing), "capture_hash": "0" * 64})
        result = self.create()
        self.assertEqual(result["state"], "records_verified_with_limitations")
        self.assertFalse(result["essential_files_complete"])
        self.assertTrue(result["manifest"]["essential_selection"]["unresolved_required"])
        receipt = restore_operational_backup(result["folder"], self.root.parent / "limited-restore")
        self.assertEqual(receipt["research_events"], 2)
        self.assertFalse(receipt["essential_files_complete"])
        self.assertFalse(receipt["operational_recovery_proved"])

    def test_private_unrelated_backup_and_profile_files_never_copied(self):
        for path in (self.root / "config/local/providers.env", self.root / ".codex/auth.json",
                self.root / "unrelated/private-note.json", self.destination / "old/must-not-recurse.json",
                self.root / "data/operations/backup_controls/old.json"):
            atomic_bytes(path, b"DO_NOT_COPY_SECRET_FIXTURE")
        result = self.create()
        originals = {row["original_path"] for row in result["manifest"]["files"]}
        self.assertNotIn(str(self.root / "config/local/providers.env"), originals)
        self.assertFalse(any("must-not-recurse" in path or "private-note" in path or ".codex" in path for path in originals))
        self.assertFalse(any(b"DO_NOT_COPY_SECRET_FIXTURE" in path.read_bytes()
            for path in Path(result["folder"]).rglob("*") if path.is_file()))
        # The reviewed public setting is the one narrow config exception.
        self.assertIn(str(self.settings), originals)

    def test_changed_selected_bytes_are_omitted_with_an_exact_copy_failure(self):
        def selecting(state, finances, **kwargs):
            selected = select_essential_backup_files(state, finances, **kwargs)
            atomic_bytes(self.code, b"VALUE = 999\n")
            return selected
        with patch("product_radar.operational_backup_selection.select_essential_backup_files", side_effect=selecting):
            result = self.create()
        selection = result["manifest"]["essential_selection"]
        self.assertEqual(selection["copy_failures"], [{"original_path": str(self.code), "kind": "application_code",
            "required": True, "reason": "BACKUP_ATTACHMENT_SOURCE_HASH_MISMATCH"}])
        self.assertEqual(result["state"], "records_verified_with_limitations")
        self.assertFalse(any(row["original_path"] == str(self.code) for row in result["manifest"]["attachments"]))
        self.assertEqual(verify_operational_backup(result["folder"]), result["manifest"])

    def test_selected_file_removed_or_symlinked_after_selection_never_followed(self):
        for replacement in ("missing", "symlink"):
            with self.subTest(replacement=replacement):
                atomic_bytes(self.code, b"VALUE = 1\n")
                outside = self.root.parent / "outside-private.txt"; atomic_bytes(outside, b"DO_NOT_FOLLOW")
                def selecting(state, finances, **kwargs):
                    selected = select_essential_backup_files(state, finances, **kwargs)
                    self.code.unlink()
                    if replacement == "symlink": self.code.symlink_to(outside)
                    return selected
                with patch("product_radar.operational_backup_selection.select_essential_backup_files", side_effect=selecting):
                    result = self.create(backup_id="essential-" + replacement)
                row = result["manifest"]["essential_selection"]["copy_failures"][0]
                self.assertEqual(row["reason"], "SYMLINK_PATH_REFUSED" if replacement == "symlink" else "BACKUP_FILE_UNAVAILABLE")
                self.assertFalse(result["essential_files_complete"])
                self.assertEqual(outside.read_bytes(), b"DO_NOT_FOLLOW")
                if self.code.is_symlink(): self.code.unlink()

    def test_changed_json_digest_is_not_accepted_just_because_byte_hash_matches(self):
        self.source()
        def selecting(state, finances, **kwargs):
            selected = select_essential_backup_files(state, finances, **kwargs)
            row = next(row for row in selected["files"] if row["relative_path"].endswith("receipt.json"))
            row["expected_json_digest"] = "0" * 64
            selected["selection_fingerprint"] = digest({name: item for name, item in selected.items() if name != "selection_fingerprint"})
            return selected
        with patch("product_radar.operational_backup_selection.select_essential_backup_files", side_effect=selecting):
            result = self.create()
        self.assertEqual(result["manifest"]["essential_selection"]["copy_failures"][0]["reason"],
            "BACKUP_ATTACHMENT_JSON_DIGEST_MISMATCH")
        self.assertFalse(result["essential_files_complete"])

    def test_per_file_copy_limit_records_a_limitation_without_discarding_healthy_core_records(self):
        # Lower the copy limit instead of creating a real oversized file. The
        # selector retains its normal bound, so this exercises the copy seam.
        maximum = len(self.memory.journal.path.read_bytes()) + 1024
        original = b"# Fictional large application file\n" + b"x" * (maximum + 1)
        atomic_bytes(self.code, original)
        with patch("product_radar.operational_backup.MAX_ATTACHMENT_BYTES", maximum):
            result = self.create()
        selection = result["manifest"]["essential_selection"]
        self.assertEqual(selection["copy_failures"], [{"original_path": str(self.code),
            "kind": "application_code", "required": True, "reason": "BACKUP_FILE_SIZE_LIMIT"}])
        self.assertFalse(result["essential_files_complete"])
        self.assertEqual(self.code.read_bytes(), original)
        self.assertEqual(verify_operational_backup(result["folder"]), result["manifest"])

    def test_resealed_checkpoint_selection_or_ready_claim_cannot_donate_false_completeness(self):
        for field, value in (("research_state_hash", "0" * 64), ("project_root_hash", "0" * 64),
                ("essential_files_complete", False), ("operational_recovery_proved", True)):
            with self.subTest(field=field):
                result = self.create(backup_id="tampered-" + field.replace("_", "-"))
                def corrupt(manifest):
                    selection = manifest["essential_selection"]; selection[field] = value
                    if field == "research_state_hash":
                        original = {name: item for name, item in selection.items() if name not in
                            {"project_root", "project_root_hash", "copy_failures", "coverage_state", "essential_files_complete", "operational_recovery_proved"}}
                        selection["selection_fingerprint"] = digest({name: item for name, item in original.items() if name != "selection_fingerprint"})
                self.reseal(result["folder"], corrupt)
                with self.assertRaises(SafetyError): verify_operational_backup(result["folder"])

    def test_copied_scope_omission_or_relabel_cannot_be_hidden_by_resealing_manifest(self):
        for mutation in ("omitted", "relabelled"):
            with self.subTest(mutation=mutation):
                result = self.create(backup_id="scope-" + mutation)
                def corrupt(manifest):
                    row = next(row for row in manifest["attachments"] if row["original_path"] == str(self.code))
                    if mutation == "omitted":
                        manifest["attachments"].remove(row)
                        manifest["files"] = [file for file in manifest["files"] if file["path"] != row["path"]]
                    else: row["kind"] = "source_capture"
                self.reseal(result["folder"], corrupt)
                with self.assertRaises(SafetyError): verify_operational_backup(result["folder"])

    def test_copy_time_core_record_corruption_never_creates_ready_manifest(self):
        event = self.memory.root / "events/00000001.json"
        def selecting(state, finances, **kwargs):
            selected = select_essential_backup_files(state, finances, **kwargs)
            value = load_json(event); value["changes"][0]["value"]["description"]["label"] = "Changed source"
            atomic_json(event, value)
            return selected
        with patch("product_radar.operational_backup_selection.select_essential_backup_files", side_effect=selecting):
            with self.assertRaises(SafetyError): self.create()
        self.assertFalse((self.destination / "essential-one/manifest.json").exists())

    def test_foreign_selector_state_is_rejected_before_copy_or_ready_manifest(self):
        def selecting(state, finances, **kwargs):
            selected = select_essential_backup_files(state, finances, **kwargs)
            selected["research_state_hash"] = "0" * 64
            selected["selection_fingerprint"] = digest({name: item for name, item in selected.items() if name != "selection_fingerprint"})
            return selected
        with patch("product_radar.operational_backup_selection.select_essential_backup_files", side_effect=selecting):
            with self.assertRaisesRegex(SafetyError, "ESSENTIAL_CHECKPOINT_CONFLICT"): self.create()
        self.assertFalse((self.destination / "essential-one").exists())

    def test_changed_copied_attachment_is_rejected_before_any_restore_write(self):
        result = self.create()
        attachment = next(row for row in result["manifest"]["attachments"] if row["original_path"] == str(self.code))
        atomic_bytes(Path(result["folder"]) / attachment["path"], b"CHANGED COPIED CODE")
        destination = self.root.parent / "never-created-restore"
        with self.assertRaisesRegex(SafetyError, "FILE_HASH_MISMATCH"):
            restore_operational_backup(result["folder"], destination)
        self.assertFalse(destination.exists())

    def test_v2_verification_and_restore_need_only_original_backup_not_lost_project_files(self):
        result = self.create(); self.code.unlink(); self.settings.unlink()
        with patch("product_radar.operational_backup_selection.select_essential_backup_files", side_effect=AssertionError("No source reselect")):
            self.assertEqual(verify_operational_backup(result["folder"]), result["manifest"])
            proof = restore_operational_backup(result["folder"], self.root.parent / "lost-source-restore")
        self.assertTrue(proof["essential_files_complete"])
        self.assertFalse(proof["operational_recovery_proved"])

    def test_existing_bounds_fail_without_overwriting_working_source_or_ready_manifest(self):
        original = self.code.read_bytes()
        for limit, cap in (("MAX_BACKUP_FILES", 1), ("MAX_BACKUP_BYTES", 1), ("MAX_MANIFEST_BYTES", 1)):
            with self.subTest(limit=limit), patch("product_radar.operational_backup." + limit, cap):
                with self.assertRaises(SafetyError): self.create(backup_id="bounded-" + limit.replace("_", "-"))
            self.assertEqual(self.code.read_bytes(), original)
            self.assertFalse((self.destination / ("bounded-" + limit.replace("_", "-")) / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
