"""Exact offline selection; no record replay, providers, credentials or backup."""

from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar.models import SafetyError, digest
from product_radar.operational_backup import _attachment
from product_radar.operational_backup import create_essential_operational_backup, restore_operational_backup
from product_radar.operational_backup_selection import (
    select_essential_backup_files, validate_backup_settings,
    validate_essential_backup_reference, validate_essential_backup_selection,
)
from product_radar.research_memory import ResearchMemory, TABLES, key
from product_radar.research_questions import physical_request_fingerprint
from product_radar.report_assets import freeze_report_assets, prepare_report_asset_plan, resolve_report_asset
from product_radar.storage import Journal, atomic_bytes, atomic_json


class EssentialBackupSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="essential-selection-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = {table: {} for table in TABLES}
        self.financial = ({}, {})
        self.memory = self.root / "data/research/captured"

    def json(self, relative, value):
        path = self.root / relative
        atomic_json(path, value)
        return path

    def raw(self, relative, value=b"public fixture bytes"):
        path = self.root / relative
        atomic_bytes(path, value)
        return path

    def select(self, **options):
        return select_essential_backup_files(self.state, self.financial, root=self.root, **options)

    def files(self, selected):
        return {row["relative_path"]: row for row in selected["files"]}

    def source_stage(self, *, parsed=False, stage_type="response", parse_state="accepted"):
        """Exact synthetic canonical owner/stages, not a source or runner."""
        from product_radar.source_acquisitions import ACQUISITION_KIND, STAGE_KIND, _record, _stage
        acquisition_id = "stage-owned-acquisition"
        folder = f"data/research/captured/supplier_routes/shared/{acquisition_id}"
        paths = {name: self.json(folder + "/" + name, {"public_fixture": name})
                 for name in ("receipt.json", "legacy-origin.json", "original.json")}
        proofs = {name: {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}
                  for name, path in paths.items()}
        request = {"country": "IN", "query": "PVC door bottom seal strip", "limit": 10,
                   "transport": "public-http", "url": "https://dir.indiamart.com/search.mp?ss=PVC+door+bottom+seal+strip"}
        at = "2026-10-11T17:31:37+00:00"
        binding = {"contract": ACQUISITION_KIND, "acquisition_id": acquisition_id,
                   "route": "indiamart", "request": request, "method": "GET", "capture_options": {},
                   "physical_request_fingerprint": physical_request_fingerprint("indiamart", request),
                   "generation": 1, "previous_acquisition_id": None, "owner_id": "stage-caller",
                   "original_attempt_id": "stage-original-attempt", "artifact_roots": [str(self.memory)]}
        owner = {"kind": ACQUISITION_KIND, "binding": binding, "binding_hash": digest(binding),
                 "state": "response_saved", "created_at": at, "updated_at": at,
                 "stage_ids": [], "response_ids": [], "parse_ids": [],
                 "active_response_id": None, "active_parse_id": None, "active_dispatch_intent_id": None}
        def save(stage_type, **fields):
            stage = {"kind": STAGE_KIND, "stage_type": stage_type, "acquisition_id": acquisition_id,
                     "binding_hash": owner["binding_hash"], **fields}
            stage_id = key("source-acquisition-stage", stage)
            stage["recorded_at"] = at
            stage["record_hash"] = digest(stage)
            self.state["source_checks"][stage_id] = stage
            owner["stage_ids"].append(stage_id)
            return stage_id
        response_fields = ({"dispatch_started": True, "captures": [], "response_hash": digest(proofs)}
                           if stage_type == "response" else {"process_proof_hash": digest({"public_fixture": "zero"})})
        response_id = save(stage_type, receipt=proofs["receipt.json"],
                           artifacts={name: proofs[name] for name in ("legacy-origin.json", "original.json")},
                           observed_at=at, **response_fields)
        if stage_type == "response":
            owner["response_ids"] = [response_id]
            owner["active_response_id"] = response_id
        else:
            owner["state"] = "reserved"
        if parsed:
            paths["parsed.json"] = self.json(folder + "/parsed.json", {
                "contract": "saved-source-parse-v1", "source_response_hash": digest(proofs),
                "parser_contract": "synthetic-source-parser-v1",
                "state": "usable" if parse_state == "accepted" else "failed", "records": []})
            proofs["parsed.json"] = {"path": str(paths["parsed.json"]),
                                     "sha256": sha256(paths["parsed.json"].read_bytes()).hexdigest()}
            parse_id = save("parse", parsed=proofs["parsed.json"], response_id=response_id,
                            response_hash=digest({name: proof for name, proof in proofs.items() if name != "parsed.json"}),
                            parser_contract="synthetic-source-parser-v1", parse_state=parse_state)
            owner.update(state="parsed" if parse_state == "accepted" else "response_saved",
                         parse_ids=[parse_id], active_parse_id=parse_id)
        self.state["batch_runs"][acquisition_id] = owner
        _record(self.state, acquisition_id)
        for stage_id in owner["stage_ids"]:
            _stage(self.state, owner, stage_id)
        return owner, paths, proofs

    def replace_source_stage(self, owner, stage_id=None, **fields):
        """Rebuild an exact fixture ID/hash after changing its saved metadata."""
        stage_id = stage_id or owner["active_response_id"] or owner["stage_ids"][0]
        stage = self.state["source_checks"].pop(stage_id)
        stage.update(fields)
        new_id = key("source-acquisition-stage", {
            field: value for field, value in stage.items() if field not in {"record_hash", "recorded_at"}})
        stage["record_hash"] = digest({field: value for field, value in stage.items() if field != "record_hash"})
        self.state["source_checks"][new_id] = stage
        for field in ("stage_ids", "response_ids", "parse_ids"):
            owner[field] = [new_id if value == stage_id else value for value in owner[field]]
        for field in ("active_response_id", "active_parse_id", "active_dispatch_intent_id"):
            if owner.get(field) == stage_id:
                owner[field] = new_id
        return new_id

    def test_canonical_source_response_receipt_and_exact_artifacts_are_selected(self):
        _, paths, proofs = self.source_stage()
        for multipart in (False, True):
            with self.subTest(multipart=multipart):
                selected = self.select(multipart=multipart)
                rows = self.files(selected)
                self.assertEqual(set(rows), {path.relative_to(self.root).as_posix() for path in paths.values()})
                for name, path in paths.items():
                    row = rows[path.relative_to(self.root).as_posix()]
                    self.assertTrue(row["required"])
                    self.assertEqual(row["expected_sha256"], proofs[name]["sha256"])
                self.assertFalse(selected["unsafe"] or selected["unresolved_required"])

    def test_canonical_source_parse_receipt_is_selected(self):
        _, paths, proofs = self.source_stage(parsed=True)
        selected = self.select(multipart=True)
        rows = self.files(selected)
        relative = paths["parsed.json"].relative_to(self.root).as_posix()
        self.assertIn(relative, rows)
        self.assertEqual(rows[relative]["expected_sha256"], proofs["parsed.json"]["sha256"])
        self.assertTrue(rows[relative]["required"])
        self.assertFalse(selected["unsafe"] or selected["unresolved_required"])

    def test_canonical_source_logical_artifact_names_preserve_exact_owned_original_paths(self):
        owner, paths, proofs = self.source_stage()
        capture = self.json(paths["original.json"].parent / "capture.json", {"public_fixture": "retained legacy capture"})
        native = self.json(paths["original.json"].parent / "native_result.json", {"public_fixture": "retained native original"})
        capture_proof = {"path": str(capture), "sha256": sha256(capture.read_bytes()).hexdigest()}
        native_proof = {"path": str(native), "sha256": sha256(native.read_bytes()).hexdigest()}
        # supplier_search._record_response owns logical labels: original.json
        # may refer to an old capture.json, and native-original-N to native proof.
        self.replace_source_stage(owner, artifacts={"legacy-origin.json": proofs["legacy-origin.json"],
            "original.json": capture_proof, "native-original-0": native_proof})
        for multipart in (False, True):
            with self.subTest(multipart=multipart):
                selected = self.select(multipart=multipart); rows = self.files(selected)
                self.assertEqual(set(rows), {path.relative_to(self.root).as_posix() for path in
                    (paths["receipt.json"], paths["legacy-origin.json"], capture, native)})
                for path, proof in ((capture, capture_proof), (native, native_proof)):
                    row = rows[path.relative_to(self.root).as_posix()]
                    self.assertEqual(row["expected_sha256"], proof["sha256"])
                    self.assertTrue(row["required"])
                self.assertFalse(selected["unsafe"] or selected["unresolved_required"])

    def test_canonical_source_failed_parse_and_prelaunch_originals_are_required(self):
        self.source_stage(parsed=True, parse_state="failed")
        selected = self.select(multipart=True)
        self.assertEqual(len(selected["files"]), 4)
        self.assertTrue(all(row["required"] for row in selected["files"]))
        self.assertFalse(selected["unsafe"] or selected["unresolved_required"])
        self.state = {table: {} for table in TABLES}
        owner, paths, proofs = self.source_stage(stage_type="prelaunch_zero")
        exit_path = self.json(paths["receipt.json"].parent / "process-exit.json", {
            "contract": "owned-source-process-exit-v1", "owned_process_exited": True, "request_sent": False})
        exit_proof = {"path": str(exit_path), "sha256": sha256(exit_path.read_bytes()).hexdigest()}
        self.replace_source_stage(owner, artifacts={"process-exit.json": exit_proof})
        selected = self.select(multipart=True); rows = self.files(selected)
        self.assertEqual(set(rows), {path.relative_to(self.root).as_posix() for path in (paths["receipt.json"], exit_path)})
        self.assertTrue(all(row["required"] for row in rows.values()))
        self.assertEqual(rows[exit_path.relative_to(self.root).as_posix()]["expected_sha256"], exit_proof["sha256"])
        self.assertFalse(selected["unsafe"] or selected["unresolved_required"])

    def test_canonical_source_stage_missing_or_changed_bytes_do_not_hide_healthy_originals(self):
        _, paths, _ = self.source_stage()
        paths["legacy-origin.json"].unlink()
        atomic_json(paths["original.json"], {"later": "different original bytes"})
        selected = self.select(multipart=True); rows = self.files(selected)
        self.assertEqual(set(rows), {paths["receipt.json"].relative_to(self.root).as_posix()})
        self.assertIn(("BACKUP_FILE_UNAVAILABLE", paths["legacy-origin.json"].relative_to(self.root).as_posix()),
            {(row["reason"], row["relative_path"]) for row in selected["unresolved_required"]})
        self.assertIn(("BACKUP_SELECTION_HASH_MISMATCH", paths["original.json"].relative_to(self.root).as_posix()),
            {(row["reason"], row["relative_path"]) for row in selected["unsafe"]})

    def test_canonical_source_every_required_proof_needs_its_exact_original_bytes(self):
        for name in ("receipt.json", "legacy-origin.json", "original.json", "parsed.json"):
            for damage in ("absent", "changed"):
                with self.subTest(name=name, damage=damage):
                    self.state = {table: {} for table in TABLES}
                    _, paths, _ = self.source_stage(parsed=True)
                    if damage == "absent":
                        paths[name].unlink()
                    else:
                        atomic_json(paths[name], {"later": "changed bytes"})
                    for multipart in (False, True):
                        selected = self.select(multipart=multipart)
                        relative = paths[name].relative_to(self.root).as_posix()
                        self.assertNotIn(relative, self.files(selected))
                        self.assertEqual(len(selected["files"]), 3)
                        limitations = selected["unresolved_required" if damage == "absent" else "unsafe"]
                        self.assertIn(("BACKUP_FILE_UNAVAILABLE" if damage == "absent" else "BACKUP_SELECTION_HASH_MISMATCH", relative),
                            {(row["reason"], row["relative_path"]) for row in limitations})

    def test_canonical_source_receipt_and_parse_need_exact_proof_shapes(self):
        for field in ("receipt", "parsed"):
            for damage in ("absent", "extra_field", "bad_hash", "not_dict"):
                with self.subTest(field=field, damage=damage):
                    self.state = {table: {} for table in TABLES}
                    owner, paths, proofs = self.source_stage(parsed=True)
                    name = "receipt.json" if field == "receipt" else "parsed.json"
                    proof = deepcopy(proofs[name])
                    if damage == "absent":
                        proof = None
                    elif damage == "extra_field":
                        proof["unowned_path"] = "config/local/login.json"
                    elif damage == "bad_hash":
                        proof["sha256"] = "not-a-hash"
                    else:
                        proof = str(paths[name])
                    self.replace_source_stage(owner,
                        owner["active_response_id"] if field == "receipt" else owner["active_parse_id"], **{field: proof})
                    # Keep the parse's exact parent ID in sync with the changed
                    # response fixture; a proof error is not a parent-ID error.
                    if field == "receipt":
                        self.replace_source_stage(owner, owner["active_parse_id"], response_id=owner["active_response_id"])
                    selected = self.select(multipart=True)
                    self.assertNotIn(paths[name].relative_to(self.root).as_posix(), self.files(selected))
                    self.assertEqual(len(selected["files"]), 3)
                    self.assertIn("BACKUP_SELECTION_SOURCE_STAGE_PROOF_INVALID", {row["reason"] for row in selected["unsafe"]})

    def test_canonical_source_stage_bad_ownership_is_limited_without_hiding_other_records(self):
        for corruption in ("owner_missing", "owner_hash", "stage_hash", "detached_stage", "response_membership"):
            with self.subTest(corruption=corruption):
                self.state = {table: {} for table in TABLES}
                owner, paths, _ = self.source_stage()
                response_id = owner["active_response_id"]
                if corruption == "owner_missing":
                    self.state["batch_runs"].clear()
                elif corruption == "owner_hash":
                    owner["binding_hash"] = "f" * 64
                elif corruption == "stage_hash":
                    self.state["source_checks"][response_id]["record_hash"] = "f" * 64
                elif corruption == "detached_stage":
                    owner.update(stage_ids=[], response_ids=[], active_response_id=None)
                else:
                    owner.update(response_ids=[], active_response_id=None)
                healthy = self.json("data/research/captured/direct_meta/healthy/receipt.json", {"public_fixture": "healthy"})
                self.state["source_checks"]["healthy-record"] = {"reference": str(healthy)}
                selected = self.select(multipart=True)
                self.assertEqual(set(self.files(selected)), {healthy.relative_to(self.root).as_posix()})
                self.assertTrue(selected["unsafe"])
                self.assertFalse(selected["unresolved_required"])

    def test_canonical_source_parse_parent_is_validated_even_if_original_bytes_are_present(self):
        for wrong in ("missing", "hash", "unlisted"):
            with self.subTest(wrong=wrong):
                self.state = {table: {} for table in TABLES}
                owner, paths, _ = self.source_stage(parsed=True)
                if wrong == "missing":
                    self.replace_source_stage(owner, owner["active_parse_id"], response_id="missing-response")
                elif wrong == "hash":
                    self.replace_source_stage(owner, owner["active_parse_id"], response_hash="f" * 64)
                else:
                    owner["parse_ids"] = []
                    owner["active_parse_id"] = None
                selected = self.select(multipart=True)
                self.assertNotIn(paths["parsed.json"].relative_to(self.root).as_posix(), self.files(selected))
                self.assertEqual(len(selected["files"]), 3)
                self.assertIn("SOURCE_ACQUISITION_STAGE_INVALID", {row["reason"] for row in selected["unsafe"]})

    def test_canonical_source_stage_proof_shapes_and_names_do_not_widen_selection(self):
        for wrong in ("missing_hash", "extra_field", "bad_hash", "not_dict", "name", "empty_name", "windows_name", "colon_name", "artifacts_list"):
            with self.subTest(wrong=wrong):
                self.state = {table: {} for table in TABLES}
                owner, paths, proofs = self.source_stage()
                proof = deepcopy(proofs["original.json"])
                name = "original.json"
                if wrong == "missing_hash":
                    proof.pop("sha256")
                elif wrong == "extra_field":
                    proof["opaque_path"] = "config/local/login.json"
                elif wrong == "bad_hash":
                    proof["sha256"] = True
                elif wrong == "not_dict":
                    proof = str(paths["original.json"])
                elif wrong == "name":
                    name = "../original.json"
                elif wrong == "empty_name":
                    name = ""
                elif wrong == "windows_name":
                    name = "..\\original.json"
                elif wrong == "colon_name":
                    name = "C:original.json"
                artifacts = {"legacy-origin.json": proofs["legacy-origin.json"], name: proof}
                if wrong == "artifacts_list":
                    artifacts = [proof]
                self.replace_source_stage(owner, artifacts=artifacts)
                selected = self.select(multipart=True)
                self.assertNotIn(paths["original.json"].relative_to(self.root).as_posix(), self.files(selected))
                self.assertIn(paths["receipt.json"].relative_to(self.root).as_posix(), self.files(selected))
                if wrong != "artifacts_list":
                    self.assertIn(paths["legacy-origin.json"].relative_to(self.root).as_posix(), self.files(selected))
                self.assertIn("BACKUP_SELECTION_SOURCE_STAGE_PROOF_INVALID", {row["reason"] for row in selected["unsafe"]})

    def test_canonical_source_stage_private_outside_owner_and_nonallowed_bytes_stay_limited(self):
        reasons = {"private": "BACKUP_SELECTION_PRIVATE_OR_RECURSIVE",
                   "outside_owner": "BACKUP_SELECTION_SOURCE_STAGE_PROOF_INVALID",
                   "secret": "BACKUP_SELECTION_PRIVATE_OR_OVERSIZED_JSON",
                   "extension": "BACKUP_SELECTION_SCOPE_INVALID", "symlink": "SYMLINK_PATH_REFUSED"}
        for wrong in ("private", "outside_owner", "secret", "extension", "symlink"):
            with self.subTest(wrong=wrong):
                self.state = {table: {} for table in TABLES}
                owner, paths, proofs = self.source_stage()
                path = paths["original.json"]
                if wrong == "private":
                    path = self.json("config/local/login.json", {"public_fixture": "never include"})
                elif wrong == "outside_owner":
                    path = self.json("media/live/supplier-captures/original.json", {"public_fixture": "not owned"})
                elif wrong == "secret":
                    atomic_json(path, {"nested": {"api_key": "fictional-private-value"}})
                elif wrong == "symlink":
                    target = self.json("config/local/private-original.json", {"public_fixture": "never follow"})
                    path.unlink(); path.symlink_to(target)
                else:
                    path = self.raw(path.with_suffix(".bin"), b"Unapproved extension even with an exact hash.")
                proof = {"path": str(path), "sha256": sha256(path.read_bytes()).hexdigest()}
                self.replace_source_stage(owner, artifacts={"legacy-origin.json": proofs["legacy-origin.json"], path.name: proof})
                selected = self.select(multipart=True); rows = self.files(selected)
                self.assertNotIn(path.relative_to(self.root).as_posix(), rows)
                self.assertEqual(len(rows), 2)
                self.assertIn(reasons[wrong], {row["reason"] for row in selected["unsafe"]})
                self.assertNotIn("fictional-private-value", str(selected))

    def test_canonical_source_stage_bodies_and_metadata_do_not_follow_opaque_paths(self):
        owner, paths, proofs = self.source_stage()
        extra = self.json(paths["receipt.json"].parent / "unlisted.json", {"public_fixture": "not an original proof"})
        atomic_json(paths["receipt.json"], {"artifacts": {"unlisted.json": {
            "path": str(extra), "sha256": sha256(extra.read_bytes()).hexdigest()}},
            "nested": {"reference": "config/local/login.json"}})
        proof = {"path": str(paths["receipt.json"]), "sha256": sha256(paths["receipt.json"].read_bytes()).hexdigest()}
        self.replace_source_stage(owner, receipt=proof, opaque={"path": str(extra)})
        selected = self.select(multipart=True)
        self.assertEqual(len(selected["files"]), 3)
        self.assertNotIn(extra.relative_to(self.root).as_posix(), self.files(selected))
        self.assertFalse(selected["unsafe"] or selected["unresolved_required"])

    def test_canonical_source_nonfile_stage_kinds_cannot_follow_opaque_proofs(self):
        for kind in ("legacy_origin", "dispatch_intent", "execution_unknown", "unknown-stage"):
            with self.subTest(kind=kind):
                self.state = {table: {} for table in TABLES}
                self.source_stage(stage_type=kind)
                selected = self.select(multipart=True)
                self.assertEqual(selected["files"], [])
                self.assertEqual({row["reason"] for row in selected["unsafe"]},
                    {"SOURCE_ACQUISITION_STAGE_INVALID"} if kind == "unknown-stage" else set())
                self.assertFalse(selected["unresolved_required"])

    def test_canonical_source_stage_selection_is_read_only_and_freezes_exact_checked_bytes(self):
        _, paths, _ = self.source_stage(parsed=True)
        before = {path: path.read_bytes() for path in paths.values()}
        original_state = deepcopy(self.state)
        with patch("product_radar.source_acquisitions._response", side_effect=AssertionError("no source replay")), \
                patch("product_radar.source_acquisitions._owners", side_effect=AssertionError("no owner scan")), \
                patch("product_radar.source_acquisitions._file", side_effect=AssertionError("use one selector read")), \
                patch.object(ResearchMemory, "snapshot", side_effect=AssertionError("no canonical replay")), \
                patch("product_radar.storage.atomic_json", side_effect=AssertionError("no writes")), \
                patch("product_radar.storage.atomic_bytes", side_effect=AssertionError("no writes")):
            pure = self.select(multipart=True)
        self.assertEqual({path: path.read_bytes() for path in paths.values()}, before)
        self.assertEqual(self.state, original_state)
        by_bytes = {raw: path for path, raw in before.items()}
        seen = []
        def sink(raw):
            seen.append(raw)
            atomic_json(by_bytes[raw], {"later": "not the frozen original"})
        selected = self.select(multipart=True, freeze_bytes=sink)
        self.assertCountEqual(seen, before.values())
        self.assertEqual(selected, pure)
        self.assertEqual(self.state, original_state)

    def test_canonical_source_stage_freeze_failure_and_interruption_propagate(self):
        self.source_stage()
        with self.assertRaisesRegex(RuntimeError, "synthetic immutable store failure"):
            self.select(multipart=True, freeze_bytes=lambda raw: (_ for _ in ()).throw(
                RuntimeError("synthetic immutable store failure")))
        with patch("product_radar.source_acquisitions._record", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.select(multipart=True)

    def test_installed_desktop_rebuild_sources_icons_and_guide_are_selected_explicitly(self):
        code = ("scripts/desktop_research_control.py", "scripts/windows/DropshipperDesktop.cs",
            "scripts/windows/DropshipperDesktop.manifest", "scripts/windows/Build-Dropshipper.ps1",
            "scripts/windows/Install-Dropshipper.ps1", "scripts/windows/Test-Dropshipper.ps1")
        assets = ("assets/windows/neuraflow-dropshipper.ico", "assets/windows/neuraflow-dropshipper.png")
        guides = ("docs/operations/windows_desktop_launcher.md", "docs/operations/implementation_2026-10-11.md",
            "docs/reviews/2026-10-10_programme_operation_audit.md",
            "docs/reviews/2026-10-11_operating_repairs_focused_review.md")
        for relative in (*code, *assets, *guides):
            self.raw(relative, b"Public desktop recovery fixture bytes.")
        self.raw("scripts/windows/unrelated.ps1", b"Unrelated file: not selected.")
        self.json("config/local/login.json", {"access_token": "fictional-private-key"})
        selected = self.select(); rows = self.files(selected)
        self.assertEqual(set(rows), set((*code, *assets, *guides)))
        self.assertEqual({rows[path]["kind"] for path in code}, {"application_code"})
        self.assertEqual({rows[path]["kind"] for path in assets}, {"public_asset"})
        self.assertEqual({rows[path]["kind"] for path in guides}, {"operator_documentation"})
        self.assertEqual(selected["unsafe"], [])
        self.assertEqual(selected["unresolved_required"], [])

    def test_exact_operating_regressions_and_transition_review_are_selected(self):
        code = ("tests/test_source_exhausted_preparation.py", "tests/test_source_preparation_independent_review.py",
            "tests/test_operation_records.py", "tests/test_research_projection_batch.py",
            "tests/test_report_print_layout.py", "tests/test_live_outputs.py")
        guide = "docs/reviews/2026-10-11_private_legacy_transition_review.md"
        expected = {relative: ("# Public recovery fixture: " + relative + "\n").encode()
                    for relative in (*code, guide)}
        for relative, raw in expected.items():
            self.raw(relative, raw)
        for relative in ("tests/test_unrelated.py", "tests/nested/test_operation_records.py",
                         "config/local/test_operation_records.py", ".codex/test_operation_records.py",
                         "docs/reviews/unrelated-review.md"):
            self.raw(relative, b"Unrelated or private fixture: never selected.")
        for multipart in (False, True):
            with self.subTest(multipart=multipart):
                selected = self.select(multipart=multipart); rows = self.files(selected)
                self.assertEqual(set(rows), set(expected))
                self.assertEqual({rows[path]["kind"] for path in code}, {"application_code"})
                self.assertEqual(rows[guide]["kind"], "operator_documentation")
                self.assertEqual(selected["schema_version"], 2 if multipart else 1)
                self.assertEqual(selected["kind"], "essential-backup-selection-v2" if multipart
                                 else "essential-backup-selection-v1")
                for relative, raw in expected.items():
                    self.assertEqual(rows[relative]["expected_sha256"], sha256(raw).hexdigest())
                    self.assertEqual(rows[relative]["bytes"], len(raw))
                    self.assertEqual(validate_essential_backup_reference(rows[relative], root=self.root), rows[relative])
                self.assertEqual(validate_essential_backup_selection(selected, root=self.root), selected)
                self.assertFalse(selected["unsafe"] or selected["unresolved_required"])

    def test_named_operating_files_freeze_same_checked_bytes_without_expanding_scope(self):
        paths = ("tests/test_source_exhausted_preparation.py", "tests/test_source_preparation_independent_review.py",
            "tests/test_operation_records.py", "docs/reviews/2026-10-11_private_legacy_transition_review.md")
        expected = {(self.root / relative): ("# Original public fixture: " + relative + "\n").encode()
                    for relative in paths}
        for path, raw in expected.items():
            self.raw(path, raw)
        self.raw("tests/test_unrelated.py", b"Must not be sent to the byte sink.")
        seen = []
        def sink(raw):
            seen.append(raw)
            original_path = next(path for path, original in expected.items() if raw == original)
            atomic_bytes(original_path, b"# Later version must not replace the selected bytes.\n")
        selected = self.select(multipart=True, freeze_bytes=sink); rows = self.files(selected)
        self.assertEqual(set(rows), set(paths))
        self.assertCountEqual(seen, expected.values())
        for path, original in expected.items():
            self.assertNotEqual(path.read_bytes(), original)
            self.assertEqual(rows[path.relative_to(self.root).as_posix()]["expected_sha256"], sha256(original).hexdigest())
        self.assertFalse((self.root / "data/backups").exists())

    def test_exact_operating_allowlist_does_not_admit_other_tests_or_private_paths(self):
        self.raw("tests/test_operation_records.py")
        original = self.files(self.select())["tests/test_operation_records.py"]
        for relative, reason in (
                ("tests/test_unrelated.py", "BACKUP_SELECTION_SCOPE_INVALID"),
                ("tests/nested/test_operation_records.py", "BACKUP_SELECTION_SCOPE_INVALID"),
                ("tests/private/test_operation_records.py", "BACKUP_SELECTION_SCOPE_INVALID"),
                ("config/local/test_operation_records.py", "BACKUP_SELECTION_SCOPE_INVALID"),
                (".codex/test_operation_records.py", "BACKUP_SELECTION_PRIVATE_OR_RECURSIVE"),
                ("tests/.private/test_operation_records.py", "BACKUP_SELECTION_PRIVATE_OR_RECURSIVE"),
                ("tests/credentials.json", "BACKUP_SELECTION_PRIVATE_OR_RECURSIVE")):
            changed = {**original, "path": str(self.root / relative), "relative_path": relative}
            with self.subTest(relative=relative), self.assertRaisesRegex(SafetyError, reason):
                validate_essential_backup_reference(changed, root=self.root)

    def test_named_operating_test_symlink_cannot_read_private_target(self):
        private = self.raw("config/local/private-test.py", b"Private fixture: do not read.")
        public = self.root / "tests/test_operation_records.py"
        public.parent.mkdir()
        public.symlink_to(private)
        selected = self.select()
        self.assertEqual(selected["files"], [])
        self.assertIn("SYMLINK_PATH_REFUSED", {row["reason"] for row in selected["unsafe"]})

    def test_explicit_legacy_public_trends_and_supplier_receipts_keep_path_checks(self):
        locations = ("data/research/captured/trends_checks/check-one/capture.json",
            "data/research/captured/supplier_searches/search-one/search.json",
            "media/live/supplier-captures/public-supplier.json")
        for index, relative in enumerate(locations):
            path = self.json(relative, {"state": "failed", "reason": "Original public capture fixture."})
            self.state["source_checks"]["legacy-" + str(index)] = {"reference": str(path)}
        self.json("media/live/supplier-captures/unreferenced.json", {"not_selected": True})
        selected = self.select()
        self.assertEqual(set(self.files(selected)), set(locations))
        self.assertFalse(selected["unsafe"] or selected["unresolved_required"])

    def test_legacy_public_directory_does_not_admit_private_json_fields(self):
        path = self.json("media/live/supplier-captures/mixed.json", {"state": "failed", "access_token": "fictional-secret"})
        self.state["source_checks"]["legacy-private"] = {"reference": str(path)}
        selected = self.select()
        self.assertEqual(selected["files"], [])
        self.assertIn("BACKUP_SELECTION_PRIVATE_OR_OVERSIZED_JSON", {row["reason"] for row in selected["unsafe"]})

    def test_default_selection_is_read_only_and_v3_sink_gets_only_same_validated_bytes(self):
        raw = b"# One original public source read.\n"
        code = self.raw("src/product_radar/one.py", raw)
        self.json("config/local/login.json", {"access_token": "fictional-private-key"})
        before = {path.relative_to(self.root).as_posix(): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        with patch("product_radar.storage.atomic_bytes", side_effect=AssertionError("Pure selection cannot write")), \
                patch("product_radar.storage.atomic_json", side_effect=AssertionError("Pure selection cannot write")):
            ordinary = self.select(multipart=True)
        self.assertEqual({path.relative_to(self.root).as_posix(): path.read_bytes()
                          for path in self.root.rglob("*") if path.is_file()}, before)
        seen = []
        def sink(original):
            seen.append(original)
            # A caller changing this source during the callback must not
            # change the already-read original supplied for freezing.
            atomic_bytes(code, b"# Later source version.\n")
        frozen = self.select(multipart=True, freeze_bytes=sink)
        self.assertEqual(seen, [raw])
        self.assertEqual(self.files(frozen)[code.relative_to(self.root).as_posix()]["expected_sha256"],
                         self.files(ordinary)[code.relative_to(self.root).as_posix()]["expected_sha256"])

    def test_freeze_sink_is_explicit_v3_only_and_invalid_private_bytes_are_never_sent(self):
        for options in ({"freeze_bytes": lambda raw: None}, {"multipart": True, "freeze_bytes": False},
                        {"multipart": True, "freeze_bytes": 1}):
            with self.subTest(options=list(options)), self.assertRaisesRegex(SafetyError, "BACKUP_SELECTION_INPUT_INVALID"):
                self.select(**options)
        self.capture(access_token="fictional-private-key")
        seen = []
        selected = self.select(multipart=True, freeze_bytes=seen.append)
        self.assertEqual(seen, [])
        self.assertEqual(selected["files"], [])
        self.assertIn("BACKUP_SELECTION_PRIVATE_OR_OVERSIZED_JSON", {row["reason"] for row in selected["unsafe"]})

    def capture(self, name="one", **receipt):
        value = {"schema_version": 1, "request": {"route": "amazon-search"}, "artifacts": {}, **receipt}
        path = self.json(f"data/research/captured/direct_marketplace/{name}/receipt.json", value)
        self.state["captures"]["capture-" + name] = {
            "source": "amazon", "capture_hash": digest(value),
            "reference": {"folder": str(path.parent), "file": "receipt.json", "receipt_hash": digest(value)},
        }
        return path, value

    def programme(self, programme_id="programme-one"):
        record = {"kind": "research-programme-v1", "programme_id": programme_id, "source_run_id": "source-one",
                  "concept_ids": [], "week": "2026-10-05", "client_id": "client-one", "release_slots": [],
                  "location": None, "drafts": {}}
        self.state["batch_runs"][key("programme", programme_id)] = record
        return record

    def owned_assets(self, issue_id="issue-one", revision=1, *, cid="concept-one", include_photo=True):
        from PIL import Image
        output = BytesIO()
        Image.new("RGB", (80, 40), "maroon").save(output, format="PNG")
        raw = output.getvalue()
        photo = self.raw("media/live/product-images/original.png", raw)
        products = [{"concept_id": cid, "image_provenance": None}]
        if include_photo:
            products[0]["image_provenance"] = {"id": "photo-" + cid, "owner_id": cid,
                "path": str(photo), "sha256": sha256(raw).hexdigest(), "source_kind": "public_product_image",
                "source_ref": "observation-one", "source_url": "https://example.com/photo.png",
                "observed_at": "2026-10-09T09:00:00Z", "relationship": "exact_product"}
        bundle = prepare_report_asset_plan(products, staging_root=self.memory / "display-staging",
            media_root=self.root / "media/live/product-images")
        receipt = freeze_report_assets(bundle, root=self.root, issue_id=issue_id, revision=revision)
        programme = self.state["batch_runs"].get(key("programme", "programme-one")) or self.programme()
        programme["drafts"][issue_id] = {"revision": revision, "complete": False}
        folder = self.root / "media/live/reports" / issue_id / ("revision-" + str(revision)) / "assets"
        display = next(iter(folder.glob("*.jpg")), None)
        return folder / "receipt.json", receipt, display

    def test_report_owned_assets_follow_only_checked_receipt_not_shared_staging_or_extra_files(self):
        receipt_path, receipt, display = self.owned_assets()
        self.raw(display.parent / "unrelated.jpg", b"not selected")
        (self.root / "media/live/product-images/original.png").unlink()
        for staged in (self.memory / "display-staging").rglob("*.jpg"):
            staged.unlink()
        selected = self.select(); rows = self.files(selected)
        self.assertEqual(set(rows), {path.relative_to(self.root).as_posix() for path in (receipt_path, display)})
        photo = rows[display.relative_to(self.root).as_posix()]
        self.assertEqual(photo["kind"], "report_package")
        self.assertTrue(photo["required"])
        self.assertEqual(photo["expected_sha256"], receipt["plan"]["entries"][0]["display"]["sha256"])
        self.assertEqual(rows[receipt_path.relative_to(self.root).as_posix()]["expected_json_digest"], digest(receipt))
        self.assertEqual(selected["unsafe"], [])
        self.assertEqual(selected["unresolved_required"], [])

    def test_report_owned_missing_photo_is_required_not_a_reason_to_recollect(self):
        _, _, display = self.owned_assets()
        display.unlink()
        selected = self.select()
        self.assertIn(display.relative_to(self.root).as_posix(), {row["relative_path"] for row in selected["unresolved_required"]})
        self.assertNotIn(display.relative_to(self.root).as_posix(), self.files(selected))

    def test_report_owned_changed_bytes_are_not_admitted(self):
        _, _, display = self.owned_assets()
        atomic_bytes(display, b"changed")
        selected = self.select()
        self.assertNotIn(display.relative_to(self.root).as_posix(), self.files(selected))
        self.assertIn("BACKUP_SELECTION_HASH_MISMATCH", {row["reason"] for row in selected["unsafe"]})

    def test_report_owned_receipt_tampering_does_not_hide_healthy_other_issue(self):
        bad, receipt, display = self.owned_assets()
        healthy, _, healthy_display = self.owned_assets("issue-two", cid="concept-two")
        receipt["plan"]["entries"][0]["transform"]["full_frame"] = False
        receipt["plan_hash"] = digest(receipt["plan"])
        receipt["record_hash"] = digest({k: v for k, v in receipt.items() if k != "record_hash"})
        atomic_json(bad, receipt)
        selected = self.select(); rows = self.files(selected)
        self.assertNotIn(display.relative_to(self.root).as_posix(), rows)
        self.assertTrue(all(path.relative_to(self.root).as_posix() in rows for path in (healthy, healthy_display)))
        self.assertIn("REPORT_DISPLAY_TRANSFORM_INVALID", {row["reason"] for row in selected["unsafe"]})

    def test_report_owned_foreign_revision_receipt_cannot_follow_another_owner(self):
        first, receipt, display = self.owned_assets()
        receipt["issue_id"] = "other-issue"
        receipt["record_hash"] = digest({k: v for k, v in receipt.items() if k != "record_hash"})
        atomic_json(first, receipt)
        selected = self.select()
        self.assertNotIn(display.relative_to(self.root).as_posix(), self.files(selected))
        self.assertIn("REPORT_ASSET_RECEIPT_INVALID", {row["reason"] for row in selected["unsafe"]})

    def test_report_owned_non_object_receipt_is_named_incomplete(self):
        path, _, display = self.owned_assets()
        atomic_json(path, ["not an owned receipt"])
        selected = self.select()
        self.assertNotIn(display.relative_to(self.root).as_posix(), self.files(selected))
        self.assertIn("REPORT_ASSET_RECEIPT_INVALID", {row["reason"] for row in selected["unsafe"]})

    def test_report_owned_child_paths_are_not_accepted_even_when_resealed(self):
        first, receipt, display = self.owned_assets()
        receipt["plan"]["entries"][0]["display"]["path"] = "../../outside.jpg"
        receipt["plan_hash"] = digest(receipt["plan"])
        receipt["record_hash"] = digest({k: v for k, v in receipt.items() if k != "record_hash"})
        atomic_json(first, receipt)
        selected = self.select()
        self.assertNotIn(display.relative_to(self.root).as_posix(), self.files(selected))
        self.assertIn("REPORT_ASSET_PLAN_INVALID", {row["reason"] for row in selected["unsafe"]})

    def test_report_owned_symlink_is_not_followed(self):
        _, _, display = self.owned_assets()
        original = self.raw("media/live/product-images/other.jpg", display.read_bytes())
        display.unlink(); display.symlink_to(original)
        selected = self.select()
        self.assertNotIn(display.relative_to(self.root).as_posix(), self.files(selected))
        self.assertIn("SYMLINK_PATH_REFUSED", {row["reason"] for row in selected["unsafe"]})

    def test_report_owned_omitted_photo_has_no_invented_child_reference(self):
        receipt, _, display = self.owned_assets(include_photo=False)
        self.assertIsNone(display)
        selected = self.select()
        self.assertEqual(set(self.files(selected)), {receipt.relative_to(self.root).as_posix()})
        self.assertEqual(selected["unsafe"], [])
        self.assertEqual(selected["unresolved_required"], [])

    def test_report_owned_identical_bytes_keep_both_revision_owners(self):
        first, _, one = self.owned_assets()
        second, _, two = self.owned_assets(revision=2)
        self.state["batch_runs"][key("programme", "programme-one")]["drafts"]["issue-one"]["retained_revision"] = 1
        selected = self.select(); rows = self.files(selected)
        self.assertEqual(one.name, two.name)
        self.assertEqual(set(rows), {path.relative_to(self.root).as_posix() for path in (first, second, one, two)})
        self.assertEqual(selected["unsafe"], [])

    def test_report_owned_assets_survive_exact_backup_restore_and_explicit_layout_copy(self):
        receipt_path, receipt, display = self.owned_assets()
        expected = display.read_bytes()
        memory = ResearchMemory(self.memory, Journal(self.root / "data/benchmarks/state/apify"), evidence_type="synthetic")
        programme_id = key("programme", "programme-one")
        memory.put("batch_runs", programme_id, self.state["batch_runs"][programme_id])
        result = create_essential_operational_backup(memory, self.root / "data/backups",
            backup_id="owned-photo-proof", root=self.root)
        self.assertTrue(result["essential_files_complete"])
        self.assertEqual(result["provider_calls"], 0)
        self.assertEqual(result["model_calls"], 0)
        receipt_path.unlink(); display.unlink()
        restored = restore_operational_backup(result["folder"], self.root / "isolated-restore")
        layout = self.root / "explicit-restored-layout"
        for mapping in restored["attachment_mapping"]:
            relative = Path(mapping["original_path"]).relative_to(self.root)
            atomic_bytes(layout / relative, Path(mapping["restored_path"]).read_bytes())
        photo = resolve_report_asset(receipt, receipt["plan"]["entries"][0]["asset_id"],
            root=layout, issue_id="issue-one", revision=1)
        self.assertEqual(photo["bytes"], expected)
        self.assertEqual(restored["provider_calls"], 0)
        self.assertEqual(restored["model_calls"], 0)
        # This test explicitly laid out the selected copies. It does not
        # claim the backup automatically moves every application reference.
        self.assertFalse(restored["operational_recovery_proved"])

    def test_supplied_states_are_not_replayed_or_changed(self):
        original = deepcopy(self.state), deepcopy(self.financial)
        self.raw("data/research/captured/events/00000001.json", b"not a selected event")
        self.raw("data/benchmarks/state/apify/events.jsonl", b"not a selected journal")
        with patch("product_radar.research_memory.ResearchMemory.snapshot", side_effect=AssertionError("no replay")), \
                patch("product_radar.storage.Journal.state", side_effect=AssertionError("no replay")):
            selected = self.select()
        self.assertEqual((self.state, self.financial), original)
        self.assertEqual(selected["files"], [])
        self.assertEqual(selected["research_state_hash"], digest(self.state))
        self.assertEqual(selected["financial_state_hash"], digest(list(self.financial)))

    def test_shared_capture_is_deduplicated_with_both_origins(self):
        path, receipt = self.capture()
        self.state["captures"]["capture-two"] = deepcopy(self.state["captures"]["capture-one"])
        selected = self.select(); rows = self.files(selected)
        self.assertEqual(list(rows), [path.relative_to(self.root).as_posix()])
        self.assertEqual(rows[list(rows)[0]]["origins"], ["captures/capture-one", "captures/capture-two"])
        self.assertEqual(selected["selected_bytes"], len(path.read_bytes()))
        self.assertEqual(rows[list(rows)[0]]["expected_json_digest"], digest(receipt))
        self.assertEqual(rows[list(rows)[0]]["expected_sha256"], sha256(path.read_bytes()).hexdigest())

    def test_artifact_closure_keeps_original_capture_and_declared_snapshot(self):
        snapshot = self.json("data/research/captured/direct_marketplace/one/snapshot.json", {"title": "Public chart"})
        self.capture(artifacts={"snapshot.json": sha256(snapshot.read_bytes()).hexdigest()})
        selected = self.select()
        self.assertIn(snapshot.relative_to(self.root).as_posix(), self.files(selected))
        self.assertEqual(selected["unsafe"], [])

    def test_changed_artifact_has_no_admitted_bytes(self):
        snapshot = self.json("data/research/captured/direct_marketplace/one/snapshot.json", {"title": "Changed"})
        self.capture(artifacts={"snapshot.json": "a" * 64})
        selected = self.select()
        self.assertNotIn(snapshot.relative_to(self.root).as_posix(), self.files(selected))
        self.assertIn("BACKUP_SELECTION_HASH_MISMATCH", {row["reason"] for row in selected["unsafe"]})

    def test_exact_owned_native_proof_leaves_are_hash_bound_source_files(self):
        folder = "data/research/captured/direct_trends/one/"
        files = [self.json(folder + "owned_process.json", {"owned_process_id": 123}),
                 self.raw(folder + "native_expression.js", b'(() => ({title: document.title}))()'),
                 self.raw(folder + "native_ready.js", b'Boolean(document.querySelector("svg"))')]
        receipt = self.json(folder + "receipt.json", {"artifacts": {
            path.name: sha256(path.read_bytes()).hexdigest() for path in files}})
        self.state["captures"]["capture-one"] = {"source": "trends", "reference": str(receipt), "capture_hash": digest({"artifacts": {
            path.name: sha256(path.read_bytes()).hexdigest() for path in files}})}
        selected = self.select(); rows = self.files(selected)
        self.assertEqual(set(rows), {path.relative_to(self.root).as_posix() for path in files + [receipt]})
        for path in files:
            row = rows[path.relative_to(self.root).as_posix()]
            self.assertEqual(row["kind"], "source_capture")
            self.assertEqual(row["expected_sha256"], sha256(path.read_bytes()).hexdigest())
            self.assertEqual(validate_essential_backup_reference(row, root=self.root), row)
        self.assertFalse(selected["unsafe"] or selected["unresolved_required"])

    def test_changed_native_proof_bytes_remain_unsafe(self):
        folder = "data/research/captured/direct_marketplace/one/"
        for name in ("owned_process.json", "native_expression.js", "native_ready.js"):
            with self.subTest(name=name):
                path = self.json(folder + name, {"owned_process_id": 123}) if name.endswith(".json") else self.raw(folder + name)
                self.capture(artifacts={name: "a" * 64})
                selected = self.select()
                self.assertNotIn(path.relative_to(self.root).as_posix(), self.files(selected))
                self.assertIn("BACKUP_SELECTION_HASH_MISMATCH", {row["reason"] for row in selected["unsafe"]})

    def test_native_proof_exception_does_not_allow_other_script_names(self):
        for name in ("other.js", "native_expression.mjs", "NATIVE_EXPRESSION.JS"):
            with self.subTest(name=name):
                path = self.raw("data/research/captured/direct_marketplace/one/" + name)
                self.capture(artifacts={name: sha256(path.read_bytes()).hexdigest()})
                selected = self.select()
                self.assertNotIn(path.relative_to(self.root).as_posix(), self.files(selected))
                self.assertIn("BACKUP_SELECTION_ARTIFACT_NAME_INVALID", {row["reason"] for row in selected["unsafe"]})
                self.state["captures"] = {"capture-one": {"source": "meta", "reference": str(path)}}
                selected = self.select()
                self.assertEqual(selected["files"], [])
                self.assertIn("BACKUP_SELECTION_SCOPE_INVALID", {row["reason"] for row in selected["unsafe"]})

    def test_native_proof_exception_does_not_expand_roots_or_read_private_files(self):
        references = ("config/local/native_expression.js", "data/backups/old/native_ready.js",
                      "data/research/captured/direct_meta/one/native-profile/native_expression.js",
                      "data/operations/backup_controls/native_ready.js", "other-project/native_ready.js")
        original_read = __import__("product_radar.operational_backup", fromlist=["_read"])._read
        for relative in references:
            with self.subTest(relative=relative):
                path = self.raw(relative)
                self.state["captures"] = {"capture-one": {"source": "meta", "reference": str(path)}}
                def guarded(candidate, limit):
                    self.assertNotEqual(Path(candidate), path)
                    return original_read(candidate, limit)
                with patch("product_radar.operational_backup_selection._read", side_effect=guarded):
                    selected = self.select()
                self.assertEqual(selected["files"], [])
                self.assertTrue(selected["unsafe"])

    def test_native_script_proof_is_not_a_report_or_catalogue_or_control_exception(self):
        for relative, kind in (("data/outputs/issue-one/revision-1/native_ready.js", "report_package"),
                               ("data/research/captured/catalogue_materials/items/one/native_expression.js", "catalogue_material"),
                               ("data/research/captured/programme_service/native_ready.js", "operating_control")):
            with self.subTest(kind=kind), self.assertRaisesRegex(SafetyError, "BACKUP_SELECTION_SCOPE_INVALID"):
                validate_essential_backup_reference({"path": str(self.root / relative), "relative_path": relative,
                    "kind": kind, "required": True, "expected_sha256": "a" * 64, "expected_json_digest": None,
                    "bytes": 1, "origins": ["fixture"]}, root=self.root)

    def test_missing_declared_file_is_required_not_optional(self):
        self.capture(artifacts={"snapshot.json": "a" * 64})
        selected = self.select()
        self.assertTrue(any(row["relative_path"].endswith("/snapshot.json") for row in selected["unresolved_required"]))
        self.assertFalse(any(row["relative_path"] and row["relative_path"].endswith("/snapshot.json") for row in selected["absent_optional"]))

    def test_unknown_unreferenced_and_future_folders_are_not_selected(self):
        current, _ = self.capture()
        future = self.json("data/research/captured/direct_marketplace/future/receipt.json", {"public": True})
        self.json("data/research/captured/ai_jobs/unreferenced-job/intent.json", {"request": {}})
        rows = self.files(self.select())
        self.assertIn(current.relative_to(self.root).as_posix(), rows)
        self.assertNotIn(future.relative_to(self.root).as_posix(), rows)
        self.assertEqual(len(rows), 1)

    def test_paths_outside_project_and_parent_traversal_are_unsafe(self):
        for reference in ("/tmp/outside.json", "data/research/captured/direct_meta/../../../../owner.json", "C:\\owner\\auth.json"):
            with self.subTest(reference=reference):
                self.state["captures"] = {"capture-one": {"source": "meta", "reference": reference}}
                with patch("product_radar.operational_backup_selection._read", wraps=__import__("product_radar.operational_backup", fromlist=["_read"])._read) as reader:
                    selected = self.select()
                self.assertTrue(selected["unsafe"])
                self.assertFalse(any(str(call.args[0]) == reference for call in reader.call_args_list))

    def test_private_registry_profile_and_backup_references_never_read(self):
        for reference in ("config/local/apify_accounts.json", "data/research/captured/direct_meta/one/native-profile/auth.json",
                          "data/research/captured/direct_meta/one/research-browser-profile/local_state.json",
                          "data/backups/old/manifest.json", "data/operations/backup_controls/control.json"):
            with self.subTest(reference=reference):
                secret = self.json(reference, {"api_key": "not a real secret fixture"})
                self.state["captures"] = {"capture-one": {"source": "meta", "reference": str(secret)}}
                original_read = __import__("product_radar.operational_backup", fromlist=["_read"])._read
                def guarded(path, limit):
                    self.assertNotEqual(Path(path), secret)
                    return original_read(path, limit)
                with patch("product_radar.operational_backup_selection._read", side_effect=guarded):
                    selected = self.select()
                self.assertEqual(selected["files"], [])
                self.assertTrue(selected["unsafe"])

    def test_sign_in_command_is_application_code_but_the_research_profile_never_is(self):
        script = self.root / "scripts/research_sign_in.py"
        script.parent.mkdir(parents=True, exist_ok=True); script.write_text("# fixture\n")
        row = validate_essential_backup_reference({"path": str(script), "relative_path": "scripts/research_sign_in.py",
            "kind": "application_code", "required": False, "expected_sha256": sha256(b"# fixture\n").hexdigest(),
            "expected_json_digest": None, "bytes": 10, "origins": ["public_application/scripts/research_sign_in.py"]}, root=self.root)
        self.assertEqual(row["relative_path"], "scripts/research_sign_in.py")
        with self.assertRaisesRegex(SafetyError, "PRIVATE_OR_RECURSIVE"):
            validate_essential_backup_reference({"path": str(self.root / "data/research/captured/trends_browser/research-browser-profile/x.json"),
                "relative_path": "data/research/captured/trends_browser/research-browser-profile/x.json", "kind": "source_capture",
                "required": False, "expected_sha256": "0" * 64, "expected_json_digest": None, "bytes": 1, "origins": ["fixture"]}, root=self.root)

    def test_symlink_leaf_and_directory_are_refused(self):
        original = self.json("data/research/captured/direct_meta/original/receipt.json", {"public": True})
        link = self.root / "data/research/captured/direct_meta/link.json"; link.symlink_to(original)
        self.state["captures"]["capture-one"] = {"source": "meta", "reference": str(link)}
        selected = self.select()
        self.assertEqual(selected["files"], [])
        self.assertIn("SYMLINK_PATH_REFUSED", {row["reason"] for row in selected["unsafe"]})

    def test_symlink_project_root_fails_before_file_or_code_probes(self):
        link = self.root / "linked-project"; link.symlink_to(self.root, target_is_directory=True)
        with patch("product_radar.operational_backup_selection._read", side_effect=AssertionError("must not read")):
            with self.assertRaisesRegex(SafetyError, "SYMLINK"):
                select_essential_backup_files(self.state, self.financial, root=link)

    def test_nonlocal_reference_is_named_not_fetched(self):
        self.state["captures"]["capture-one"] = {"source": "meta", "reference": "https://example.com/public"}
        selected = self.select()
        self.assertEqual(selected["unresolved_required"][0]["reason"], "BACKUP_SELECTION_NONLOCAL_REFERENCE")
        self.assertEqual(selected["files"], [])

    def test_secret_fields_in_an_owned_json_are_not_selected(self):
        path = self.json("data/research/captured/direct_meta/one/receipt.json", {"headers": {"Authorization": "fictional"}})
        self.state["captures"]["capture-one"] = {"source": "meta", "reference": str(path)}
        selected = self.select()
        self.assertEqual(selected["files"], [])
        self.assertTrue(selected["unsafe"])
        self.assertNotIn("fictional", str(selected))

    def test_malformed_json_is_local_and_a_healthy_sibling_still_selected(self):
        self.raw("data/research/captured/direct_meta/bad/receipt.json", b'{"duplicated":1,"duplicated":2}')
        self.state["captures"]["capture-bad"] = {"source": "meta", "reference": str(self.memory / "direct_meta/bad/receipt.json")}
        current, _ = self.capture()
        selected = self.select()
        self.assertIn(current.relative_to(self.root).as_posix(), self.files(selected))
        self.assertTrue(selected["unsafe"])

    def test_invalid_artifact_shapes_are_named_not_crashes(self):
        for artifacts in (["bad"], {"../../outside.json": "a" * 64}, {"snapshot.json": 4}):
            with self.subTest(artifacts=artifacts):
                self.capture(artifacts=artifacts)
                selected = self.select()
                self.assertTrue(selected["unsafe"])

    def test_model_original_actual_retries_and_pending_intents_are_retained(self):
        request = {"role": "E", "payload": {"items": []}}
        self.state["jobs"]["job-one"] = {"request": request, "binding_keys": {}}
        self.json("data/research/captured/ai_jobs/job-one/intent.json", {"request": request})
        self.json("data/research/captured/ai_jobs/job-one/failure.json", {"code": "LOCAL_FAILURE"})
        self.json("data/research/captured/ai_jobs/job-one/retries.json", [{"reason": "One deliberate retry"}])
        self.json("data/research/captured/ai_jobs/job-one/retry-1/intent.json", {"request": request})
        rows = self.files(self.select())
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row["kind"] == "model_receipt" for row in rows.values()))

    def test_missing_retry_pointer_still_preserves_actual_original_retry(self):
        request = {"role": "E", "payload": {"items": []}}
        self.state["jobs"]["job-one"] = {"request": request}
        intent = self.json("data/research/captured/ai_jobs/job-one/retry-1/intent.json", {"request": request})
        selected = self.select()
        self.assertIn(intent.relative_to(self.root).as_posix(), self.files(selected))
        self.assertIn("BACKUP_SELECTION_RETRY_POINTER_MISSING", {row["reason"] for row in selected["unresolved_required"]})

    def test_changed_model_request_is_not_reported_as_complete_recovery(self):
        self.state["jobs"]["job-one"] = {"request": {"role": "E"}}
        self.json("data/research/captured/ai_jobs/job-one/intent.json", {"request": {"role": "F"}})
        selected = self.select()
        self.assertIn("BACKUP_SELECTION_JOB_REQUEST_CHANGED", {row["reason"] for row in selected["unsafe"]})

    def test_required_job_with_no_saved_artifact_is_not_hidden_by_dedup(self):
        self.state["jobs"]["job-one"] = {"request": {"role": "E"}}
        self.state["assessments"]["concept-one"] = {"job_id": "job-one"}
        selected = self.select()
        self.assertIn("BACKUP_SELECTION_JOB_ARTIFACTS_MISSING", {row["reason"] for row in selected["unresolved_required"]})

    def test_derived_model_result_follows_original_source_job(self):
        self.state["jobs"]["job-derived"] = {"request": {"role": "G"}}
        self.json("data/research/captured/ai_jobs/job-derived/derived_result.json", {"source_jobs": ["job-source"]})
        result = self.json("data/research/captured/ai_jobs/job-source/result.json", {"response": {"public": True}})
        selected = self.select()
        self.assertIn(result.relative_to(self.root).as_posix(), self.files(selected))

    def test_controller_pointer_follows_same_original_request_only(self):
        receipt = {"schema_version": 1, "route": "amazon-search", "acquisition_id": "acquisition-one",
                   "reference": str(self.memory / "direct_marketplace/one/receipt.json"), "target_results": []}
        path, _ = self.capture()
        self.state["query_tasks"]["task-one"] = {"acquisition_id": "acquisition-one", "route": "amazon-search", "request_hash": "a" * 64}
        base = "data/research/captured/all_source_dispatch/returned_receipts/acquisition-one/"
        self.json(base + "pending.json", {"receipt_hash": digest(receipt), "request_hash": "a" * 64,
                                          "route": "amazon-search", "acquisition_id": "acquisition-one", "task_id": "task-one"})
        saved = self.json(base + digest(receipt) + ".json", receipt)
        selected = self.select()
        self.assertIn(saved.relative_to(self.root).as_posix(), self.files(selected))
        self.assertIn(path.relative_to(self.root).as_posix(), self.files(selected))

    def shared_pointer(self, *, prefix="source", legacy=True):
        """An old caller and exact shared caller; no source or backup runner."""
        from product_radar.source_acquisitions import ACQUISITION_KIND, STAGE_KIND, _record, _stage
        aid = "source-acquisition-051dee1224a6e1d8b0368476d5a609f4ca2bd024"
        run_id = "source-followon-093fe9a40e2340030e3266f2b6a7a0707ff03c00"
        physical = {"country": "IN", "query": "PVC door bottom seal strip", "limit": 10,
                    "transport": "public-http", "url": "https://dir.indiamart.com/search.mp?ss=PVC+door+bottom+seal+strip"}
        short = {"country": "IN", "query": "PVC door bottom seal strip"} if legacy else deepcopy(physical)
        old_run = run_id if legacy else "source-followon-earlier"
        old_id = key("source-task", [old_run, "indiamart", short])
        named_id = key("source-task", [run_id, "indiamart", physical])
        at = "2026-10-11T17:31:37+00:00"
        old = {"kind": "all_source_task_v1", "run_id": old_run, "route": "indiamart", "source": "supplier",
               "request": short, "request_hash": digest(short), "acquisition_id": aid,
               "status": "insufficient", "generation": 1, "tries": 1, "revision": 4,
               "dispatch_started": True, "receipt_refs": [], "targets": {"concept-old": "a" * 64}}
        contract = "shared-source-caller-v1" if prefix == "source" else "shared-marketplace-caller-v1"
        named = {"kind": "all_source_task_v1", "run_id": run_id, "route": "indiamart", "source": "supplier",
                 "request": physical, "request_hash": digest(physical), "acquisition_id": aid,
                 "acquisition_owner_ref": aid, prefix + "_dispatch_contract": contract,
                 prefix + "_physical_request": deepcopy(physical), "generation": 0,
                 "reused_acquisition_ref": {"kind": ACQUISITION_KIND, "id": aid},
                 "status": "insufficient", "receipt_refs": [], "targets": {"concept-new": "b" * 64},
                 "supplier_original_pending": True}
        if not legacy:
            old.update(acquisition_owner_ref=aid, **{prefix + "_dispatch_contract": contract,
                       prefix + "_physical_request": deepcopy(physical)})
        self.state["query_tasks"].update({old_id: old, named_id: named})
        binding = {"contract": ACQUISITION_KIND, "acquisition_id": aid, "route": "indiamart",
                   "request": deepcopy(physical), "method": "GET", "capture_options": {},
                   "physical_request_fingerprint": physical_request_fingerprint("indiamart", physical),
                   "generation": 1, "previous_acquisition_id": None, "owner_id": named_id,
                   "original_attempt_id": "supplier-route-original", "artifact_roots": [str(self.memory)]}
        owner = {"kind": ACQUISITION_KIND, "binding": binding, "binding_hash": digest(binding),
                 "state": "execution_unknown", "created_at": at, "updated_at": at, "stage_ids": [],
                 "response_ids": [], "parse_ids": [], "active_response_id": None, "active_parse_id": None,
                 "active_dispatch_intent_id": None}
        if legacy:
            origin = {"table": "query_tasks", "id": old_id, "hash": digest(old),
                      "original": deepcopy(old), "question_id": None}
            stage = {"kind": STAGE_KIND, "stage_type": "legacy_origin", "acquisition_id": aid,
                     "binding_hash": owner["binding_hash"], "origin": origin}
            stage_id = key("source-acquisition-stage", stage)
            stage["recorded_at"] = at; stage["record_hash"] = digest(stage)
            owner["stage_ids"] = [stage_id]; self.state["source_checks"][stage_id] = stage
        self.state["batch_runs"][aid] = owner
        _record(self.state, aid)
        for stage_id in owner["stage_ids"]:
            _stage(self.state, owner, stage_id)
        capture = self.json(f"data/research/captured/supplier_routes/{aid}/capture.json", {"state": "insufficient", "records": []})
        receipt = {"schema_version": 1, "route": "indiamart", "acquisition_id": aid,
                   "reference": str(capture), "target_results": []}
        base = f"data/research/captured/all_source_dispatch/returned_receipts/{aid}/"
        pointer = {"receipt_hash": digest(receipt), "request_hash": named["request_hash"], "task_id": named_id,
                   "route": "indiamart", "acquisition_id": aid, "envelope_hash": digest({"public": "pending"})}
        pending = self.json(base + "pending.json", pointer)
        returned = self.json(base + digest(receipt) + ".json", receipt)
        return {"old_id": old_id, "named_id": named_id, "old": old, "named": named, "owner": owner,
                "physical": physical, "pointer": pointer, "pending": pending, "returned": returned, "capture": capture}

    def test_shared_legacy_pointer_preserves_exact_original_short_request(self):
        fixture = self.shared_pointer()
        self.assertNotEqual(fixture["old"]["request_hash"], fixture["pointer"]["request_hash"])
        self.assertEqual(fixture["old_id"], "source-task-6feed90a61119aa5d74df6c2ee59a9b4a3cc2b2c")
        self.assertEqual(fixture["named_id"], "source-task-c79e9f8c96b2cc644bcfcfcfd156f1fef1104050")
        before = deepcopy(self.state)
        selected = self.select()
        self.assertFalse(selected["unsafe"])
        self.assertFalse(selected["unresolved_required"])
        self.assertEqual(before, self.state)
        for name in ("pending", "returned", "capture"):
            self.assertIn(fixture[name].relative_to(self.root).as_posix(), self.files(selected))

    def test_shared_pointer_accepts_recognized_source_and_marketplace_callers(self):
        for prefix in ("source", "marketplace"):
            with self.subTest(prefix=prefix):
                self.state = {table: {} for table in TABLES}
                fixture = self.shared_pointer(prefix=prefix, legacy=False)
                self.assertNotEqual(fixture["old_id"], fixture["named_id"])
                self.assertEqual(fixture["old"]["request_hash"], fixture["named"]["request_hash"])
                self.assertNotEqual(fixture["old"]["targets"], fixture["named"]["targets"])
                selected = self.select()
                self.assertFalse(selected["unsafe"] or selected["unresolved_required"])

    def test_shared_pointer_accepts_consistent_dual_contract_not_arbitrary_pair(self):
        fixture = self.shared_pointer(legacy=False)
        for task in (fixture["old"], fixture["named"]):
            task.update(marketplace_dispatch_contract="shared-marketplace-caller-v1",
                        marketplace_physical_request=deepcopy(fixture["physical"]))
        self.assertFalse(self.select()["unsafe"])

    def test_shared_pointer_pending_before_canonical_apply_is_retained_not_completed(self):
        fixture = self.shared_pointer()
        before = deepcopy(self.state)
        from product_radar.source_acquisitions import _record, _stage
        with patch("product_radar.source_acquisitions._record", wraps=_record) as record, \
                patch("product_radar.source_acquisitions._stage", wraps=_stage) as stage, \
                patch("product_radar.source_acquisitions._owners", side_effect=AssertionError("no owner enumeration")), \
                patch("product_radar.source_acquisitions._response", side_effect=AssertionError("no response refresh")), \
                patch.object(ResearchMemory, "snapshot", side_effect=AssertionError("no history replay")):
            selected = self.select()
        self.assertFalse(selected["unsafe"] or selected["unresolved_required"])
        self.assertTrue(record.called and stage.called)
        self.assertTrue(all(call.args[0] is self.state for call in record.call_args_list + stage.call_args_list))
        self.assertEqual(before, self.state)
        self.assertEqual(fixture["owner"]["state"], "execution_unknown")
        self.assertTrue(fixture["named"]["supplier_original_pending"])
        self.assertFalse(fixture["named"]["receipt_refs"])

    def test_shared_pointer_frozen_resume_keeps_incomplete_owner_and_canonical_receipt(self):
        from product_radar.source_acquisitions import STAGE_KIND
        fixture = self.shared_pointer()
        old_ref, named_ref = "source-receipt-old", "source-receipt-shared"
        fixture["old"]["receipt_refs"] = [old_ref]
        stage = self.state["source_checks"][fixture["owner"]["stage_ids"][0]]
        self.replace_source_stage(fixture["owner"], origin={**stage["origin"],
            "original": deepcopy(fixture["old"]), "hash": digest(fixture["old"])})
        fixture["named"].update(receipt_refs=[named_ref], revision=3, target_results={})
        for ref, task_id, task in ((old_ref, fixture["old_id"], fixture["old"]),
                                  (named_ref, fixture["named_id"], fixture["named"])):
            self.state["source_checks"][ref] = {"kind": "all_source_receipt_v1", "task_id": task_id,
                "route": "indiamart", "request_hash": task["request_hash"], "envelope_hash": fixture["pointer"]["envelope_hash"]}
        owner = fixture["owner"]
        response = self.json(fixture["capture"].parent / "capture-parser-fixture.json", {
            "bridge": "verified-supplier-search-response-v1", "complete": False, "state": "insufficient"})
        proof = {"path": str(response), "sha256": sha256(response.read_bytes()).hexdigest()}
        original = {"path": str(fixture["capture"]), "sha256": sha256(fixture["capture"].read_bytes()).hexdigest()}
        stage = {"kind": STAGE_KIND, "stage_type": "response", "acquisition_id": owner["binding"]["acquisition_id"],
                 "binding_hash": owner["binding_hash"], "receipt": proof, "artifacts": {"original.json": original},
                 "dispatch_started": True, "captures": [], "observed_at": owner["updated_at"],
                 "response_hash": digest([proof, original])}
        stage_id = key("source-acquisition-stage", stage)
        stage["recorded_at"] = owner["updated_at"]; stage["record_hash"] = digest(stage)
        self.state["source_checks"][stage_id] = stage; owner["stage_ids"].append(stage_id)
        owner.update(state="response_saved", response_ids=[stage_id], active_response_id=stage_id)
        before = deepcopy(self.state)
        selected = self.select(multipart=True)
        self.assertFalse(selected["unsafe"] or selected["unresolved_required"])
        self.assertEqual(before, self.state)
        self.assertEqual(fixture["owner"]["state"], "response_saved")
        self.assertTrue(fixture["named"]["supplier_original_pending"])
        self.assertIn(response.relative_to(self.root).as_posix(), self.files(selected))
        self.assertIn(fixture["capture"].relative_to(self.root).as_posix(), self.files(selected))

    def test_direct_legacy_pointer_without_task_id_remains_exact(self):
        fixture = self.shared_pointer()
        pointer = {**fixture["pointer"], "request_hash": fixture["old"]["request_hash"]}
        pointer.pop("task_id")
        self.state["query_tasks"].pop(fixture["named_id"])
        self.state["batch_runs"].clear(); self.state["source_checks"].clear()
        self.json(fixture["pending"].relative_to(self.root), pointer)
        selected = self.select()
        self.assertFalse(selected["unsafe"])
        self.assertIn(fixture["returned"].relative_to(self.root).as_posix(), self.files(selected))

    def test_different_named_task_with_same_hash_still_needs_shared_proof(self):
        fixture = self.shared_pointer(legacy=False)
        fixture["named"].pop("source_dispatch_contract")
        fixture["named"].pop("source_physical_request")
        selected = self.select()
        self.assertIn("BACKUP_SELECTION_SOURCE_POINTER_CONTRACT_INVALID", {row["reason"] for row in selected["unsafe"]})

    def test_equal_hash_and_acquisition_without_referencing_caller_proof_are_not_enough(self):
        fixture = self.shared_pointer(legacy=False)
        fixture["old"].pop("source_dispatch_contract")
        fixture["old"].pop("source_physical_request")
        selected = self.select()
        self.assertIn("BACKUP_SELECTION_SOURCE_POINTER_ORIGIN_UNVERIFIED", {row["reason"] for row in selected["unsafe"]})

    def test_shared_pointer_validation_cache_is_only_for_this_supplied_selection(self):
        from product_radar.source_acquisitions import _record
        fixture = self.shared_pointer(legacy=False)
        third = deepcopy(fixture["old"])
        third["run_id"] = "source-followon-third"
        third_id = key("source-task", [third["run_id"], third["route"], third["request"]])
        self.state["query_tasks"][third_id] = third
        with patch("product_radar.source_acquisitions._record", wraps=_record) as record:
            self.assertFalse(self.select()["unsafe"])
            self.assertEqual(record.call_count, 1)
            fixture["owner"]["binding_hash"] = "a" * 64
            selected = self.select()
            self.assertEqual(record.call_count, 2)
        self.assertEqual(sum(row["reason"] == "BACKUP_SELECTION_SOURCE_POINTER_OWNER_INVALID"
                             for row in selected["unsafe"]), 2)

    def test_shared_pointer_bad_named_caller_or_pointer_is_explicit_and_siblings_continue(self):
        for case in ("missing", "kind", "digest", "request_identity", "run_identity", "route", "acquisition",
                     "pointer_digest", "pointer_route", "pointer_acquisition", "pointer_task_type", "pointer_receipt_hash"):
            with self.subTest(case=case):
                self.state = {table: {} for table in TABLES}
                fixture = self.shared_pointer()
                named = fixture["named"]; pointer = fixture["pointer"]
                if case == "missing": self.state["query_tasks"].pop(fixture["named_id"])
                elif case == "kind": named["kind"] = "other-task"
                elif case == "digest": named["request_hash"] = "a" * 64
                elif case == "request_identity":
                    named["request"] = {**named["request"], "query": "different type"}
                    named["request_hash"] = digest(named["request"])
                elif case == "run_identity": named["run_id"] = "different-run"
                elif case == "route": named["route"] = "tradeindia"
                elif case == "acquisition": named["acquisition_id"] = "different-acquisition"
                elif case == "pointer_digest": pointer["request_hash"] = "a" * 64
                elif case == "pointer_route": pointer["route"] = "tradeindia"
                elif case == "pointer_acquisition": pointer["acquisition_id"] = "different-acquisition"
                elif case == "pointer_task_type": pointer["task_id"] = {"unexpected": "object"}
                else: pointer["receipt_hash"] = "not-a-content-hash"
                self.json(fixture["pending"].relative_to(self.root), pointer)
                sibling, _ = self.capture()
                selected = self.select()
                self.assertTrue(any(row["origin"] == "query_tasks/" + fixture["old_id"]
                    and row["reason"].startswith("BACKUP_SELECTION_SOURCE_POINTER_") for row in selected["unsafe"]))
                self.assertIn(sibling.relative_to(self.root).as_posix(), self.files(selected))

    def test_shared_pointer_referencing_identity_is_checked_even_with_rehashed_snapshot(self):
        for case in ("kind", "digest", "request_identity", "run_identity"):
            with self.subTest(case=case):
                self.state = {table: {} for table in TABLES}
                fixture = self.shared_pointer(); old = fixture["old"]
                if case == "kind": old["kind"] = "other-task"
                elif case == "digest": old["request_hash"] = "a" * 64
                elif case == "request_identity":
                    old["request"] = {**old["request"], "query": "different type"}
                    old["request_hash"] = digest(old["request"])
                else: old["run_id"] = "different-run"
                stage = self.state["source_checks"][fixture["owner"]["stage_ids"][0]]
                self.replace_source_stage(fixture["owner"], origin={**stage["origin"],
                    "original": deepcopy(old), "hash": digest(old)})
                selected = self.select()
                self.assertIn("BACKUP_SELECTION_SOURCE_POINTER_CALLER_INVALID", {row["reason"] for row in selected["unsafe"]})

    def test_shared_pointer_changed_legacy_fields_are_not_discarded(self):
        changes = {"targets": {"concept-old": "c" * 64}, "revision": 5, "generation": 2,
                   "receipt_refs": ["source-receipt-changed"], "status": "acquired", "tries": 2,
                   "target_results": {"concept-old": {"state": "usable"}}, "extra_runtime_field": True}
        for field, value in changes.items():
            with self.subTest(field=field):
                self.state = {table: {} for table in TABLES}
                fixture = self.shared_pointer(); fixture["old"][field] = value
                selected = self.select()
                self.assertIn("BACKUP_SELECTION_SOURCE_POINTER_ORIGIN_UNVERIFIED", {row["reason"] for row in selected["unsafe"]})

    def test_shared_pointer_missing_or_changed_origin_proof_is_not_guessed(self):
        for case in ("absent", "no_original", "wrong_hash", "wrong_table", "wrong_id", "changed_original", "stage_hash"):
            with self.subTest(case=case):
                self.state = {table: {} for table in TABLES}
                fixture = self.shared_pointer(); owner = fixture["owner"]
                stage_id = owner["stage_ids"][0]; stage = self.state["source_checks"][stage_id]
                origin = deepcopy(stage["origin"])
                if case == "absent":
                    self.state["source_checks"].pop(stage_id); owner["stage_ids"] = []
                elif case == "stage_hash": stage["origin"]["hash"] = "a" * 64
                else:
                    if case == "no_original": origin.pop("original")
                    elif case == "wrong_hash": origin["hash"] = "a" * 64
                    elif case == "wrong_table": origin["table"] = "batch_runs"
                    elif case == "wrong_id": origin["id"] = fixture["named_id"]
                    else:
                        origin["original"]["tries"] = 7; origin["hash"] = digest(origin["original"])
                    self.replace_source_stage(owner, origin=origin)
                selected = self.select()
                expected = ("BACKUP_SELECTION_SOURCE_POINTER_ORIGIN_INVALID" if case == "stage_hash"
                            else "BACKUP_SELECTION_SOURCE_POINTER_ORIGIN_UNVERIFIED")
                self.assertIn(expected, {row["reason"] for row in selected["unsafe"]})

    def test_shared_pointer_missing_or_changed_owner_is_not_shared_proof(self):
        for case in ("missing", "binding_hash", "fingerprint"):
            with self.subTest(case=case):
                self.state = {table: {} for table in TABLES}
                fixture = self.shared_pointer(); owner = fixture["owner"]
                if case == "missing": self.state["batch_runs"].clear()
                elif case == "binding_hash": owner["binding_hash"] = "a" * 64
                else:
                    owner["binding"]["physical_request_fingerprint"] = "a" * 64
                    owner["binding_hash"] = digest(owner["binding"])
                selected = self.select()
                self.assertIn("BACKUP_SELECTION_SOURCE_POINTER_OWNER_INVALID", {row["reason"] for row in selected["unsafe"]})

    def test_shared_pointer_owner_path_error_is_isolated_from_healthy_siblings(self):
        fixture = self.shared_pointer()
        sibling, _ = self.capture()
        with patch("product_radar.source_acquisitions._record", side_effect=PermissionError("private path detail")):
            selected = self.select()
        self.assertIn("BACKUP_SELECTION_SOURCE_POINTER_OWNER_INVALID", {row["reason"] for row in selected["unsafe"]})
        self.assertIn(sibling.relative_to(self.root).as_posix(), self.files(selected))
        self.assertIn(fixture["returned"].relative_to(self.root).as_posix(), self.files(selected))
        self.assertNotIn("private path detail", str(selected))

    def test_shared_pointer_interruption_and_backup_freeze_errors_still_propagate(self):
        self.shared_pointer(legacy=False)
        with patch("product_radar.source_acquisitions._record", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.select()
        def failed_freeze(raw):
            raise PermissionError("backup object save failed")
        with self.assertRaisesRegex(PermissionError, "backup object save failed"):
            self.select(multipart=True, freeze_bytes=failed_freeze)

    def test_shared_pointer_contract_and_physical_scope_must_be_exact_for_both_callers(self):
        for caller in ("old", "named"):
            for case in ("unknown", "missing_marker", "missing_physical", "owner_ref", "country", "query", "limit",
                         "transport", "url", "conflicting_marketplace", "orphan_marketplace"):
                with self.subTest(caller=caller, case=case):
                    self.state = {table: {} for table in TABLES}
                    fixture = self.shared_pointer(legacy=False); task = fixture[caller]
                    if case == "unknown": task["source_dispatch_contract"] = "other-contract"
                    elif case == "missing_marker": task.pop("source_dispatch_contract")
                    elif case == "missing_physical": task.pop("source_physical_request")
                    elif case == "owner_ref": task["acquisition_owner_ref"] = "different-acquisition"
                    elif case == "orphan_marketplace": task["marketplace_physical_request"] = deepcopy(fixture["physical"])
                    elif case == "conflicting_marketplace":
                        task.update(marketplace_dispatch_contract="shared-marketplace-caller-v1",
                                    marketplace_physical_request={**fixture["physical"], "limit": 11})
                    else:
                        changes = {"country": "US", "query": "different type", "limit": 11,
                                   "transport": "public-browser", "url": "https://dir.indiamart.com/different"}
                        task["source_physical_request"][case] = changes[case]
                    selected = self.select()
                    self.assertIn("BACKUP_SELECTION_SOURCE_POINTER_CONTRACT_INVALID", {row["reason"] for row in selected["unsafe"]})

    def test_shared_pointer_nondefault_owner_method_or_options_need_specific_caller_proof(self):
        for fields in ({"method": "POST"}, {"capture_options": {"render": True}}):
            with self.subTest(fields=fields):
                self.state = {table: {} for table in TABLES}
                fixture = self.shared_pointer(legacy=False); owner = fixture["owner"]
                owner["binding"].update(fields)
                binding = owner["binding"]
                binding["physical_request_fingerprint"] = physical_request_fingerprint(binding["route"], binding["request"],
                    method=binding["method"], capture_options=binding["capture_options"])
                owner["binding_hash"] = digest(binding)
                selected = self.select()
                self.assertIn("BACKUP_SELECTION_SOURCE_POINTER_CONTRACT_INVALID", {row["reason"] for row in selected["unsafe"]})

    def test_shared_pointer_does_not_hide_missing_canonical_receipt(self):
        fixture = self.shared_pointer(legacy=False)
        fixture["old"]["receipt_refs"] = ["source-receipt-not-saved"]
        selected = self.select()
        self.assertFalse(selected["unsafe"])
        self.assertIn("BACKUP_SELECTION_CANONICAL_RECEIPT_MISSING", {row["reason"] for row in selected["unresolved_required"]})

    def test_shared_pointer_changed_returned_bytes_still_fail_hash_closure(self):
        fixture = self.shared_pointer()
        self.json(fixture["returned"].relative_to(self.root), {"changed": "returned receipt"})
        selected = self.select()
        self.assertIn("BACKUP_SELECTION_HASH_MISMATCH", {row["reason"] for row in selected["unsafe"]})
        self.assertNotIn(fixture["returned"].relative_to(self.root).as_posix(), self.files(selected))
        self.assertIn(fixture["capture"].relative_to(self.root).as_posix(), self.files(selected))

    def test_shared_pointer_missing_returned_receipt_still_needs_required_original(self):
        fixture = self.shared_pointer()
        fixture["returned"].unlink()
        selected = self.select()
        self.assertIn("BACKUP_FILE_UNAVAILABLE", {row["reason"] for row in selected["unresolved_required"]})
        self.assertFalse(selected["unsafe"])

    def test_changed_controller_pointer_is_a_named_unsafe_binding(self):
        self.state["query_tasks"]["task-one"] = {"acquisition_id": "acquisition-one", "route": "amazon-search", "request_hash": "a" * 64}
        self.json("data/research/captured/all_source_dispatch/returned_receipts/acquisition-one/pending.json",
                  {"request_hash": "b" * 64, "route": "amazon-search", "acquisition_id": "acquisition-one"})
        selected = self.select()
        self.assertIn("BACKUP_SELECTION_SOURCE_POINTER_CHANGED", {row["reason"] for row in selected["unsafe"]})

    def test_uncertain_acquisition_keeps_native_proof_before_outer_import(self):
        self.state["query_tasks"]["task-one"] = {"acquisition_id": "acquisition-one", "route": "blinkit", "request_hash": "a" * 64,
                                                 "reconcile_required": True}
        original = self.json("data/research/captured/quickcommerce_free/acquisitions/acquisition-one/native/native_outer_deadline.json",
                             {"navigation_started": None, "reason": "SOURCE_ACTION_TIMEOUT"})
        selected = self.select()
        self.assertIn(original.relative_to(self.root).as_posix(), self.files(selected))

    def test_supplier_search_native_and_company_bundle_closure(self):
        native = self.json("data/research/captured/supplier_routes/native/request-one/native_result.json", {"navigation_started": True})
        bundle = self.json("data/research/captured/supplier_pages/pass-one/capture.json",
                           {"bundle": {"receipts": [{"native_reference": str(native)}]}})
        route = self.json("data/research/captured/supplier_routes/acquisition-one/capture.json",
                          {"records": [{"receipt": {"native_reference": str(native)}}]})
        self.state["source_checks"]["supplier-check"] = {"reference": str(route), "receipt_reference": str(bundle)}
        rows = self.files(self.select())
        self.assertEqual(len(rows), 3)
        self.assertIn(native.relative_to(self.root).as_posix(), rows)
        self.assertEqual(len(rows[native.relative_to(self.root).as_posix()]["origins"]), 2)

    def test_explicit_unsaved_failed_native_result_is_optional_and_retains_original_proof(self):
        folder = "data/research/captured/supplier_routes/native/request-one/"
        missing = self.root / folder / "native_result.json"
        dispatch = self.json(folder + "native_dispatch.json", {"navigation_started": False})
        owned = self.json(folder + "owned_process.json", {"owned_process_id": 123})
        expression = self.raw(folder + "native_expression.js", b"Public browser proof fixture.")
        receipt = {"state": "unavailable", "native_result_saved": False, "native_reference": str(missing)}
        for shape in (receipt, {"records": [{"receipt": receipt}]}, {"bundle": {"receipts": [receipt]}}):
            with self.subTest(shape=next(iter(shape))):
                capture = self.json("data/research/captured/supplier_routes/acquisition-one/capture.json", shape)
                self.state["source_checks"]["supplier-check"] = {"reference": str(capture)}
                selected = self.select()
                self.assertEqual(set(self.files(selected)), {
                    path.relative_to(self.root).as_posix() for path in (capture, dispatch, owned, expression)})
                self.assertIn(missing.relative_to(self.root).as_posix(),
                              {row["relative_path"] for row in selected["absent_optional"]})
                self.assertFalse(selected["unresolved_required"] or selected["unsafe"])

    def test_unsaved_result_does_not_waive_legacy_success_or_wrong_filename(self):
        folder = "data/research/captured/supplier_routes/native/request-one/"
        for declaration in ({"state": "unavailable"}, {"state": "unavailable", "native_result_saved": True},
                            {"state": "unavailable", "native_result_saved": 0},
                            {"state": "usable", "native_result_saved": False},
                            {"state": [], "native_result_saved": False},
                            {"state": "failed", "native_result_saved": False, "filename": "snapshot.json"}):
            with self.subTest(declaration=declaration):
                missing = self.root / folder / declaration.get("filename", "native_result.json")
                value = {key: value for key, value in declaration.items() if key != "filename"}
                capture = self.json("data/research/captured/supplier_routes/acquisition-one/capture.json",
                                    {**value, "native_reference": str(missing)})
                self.state["source_checks"]["supplier-check"] = {"reference": str(capture)}
                selected = self.select()
                self.assertIn(missing.relative_to(self.root).as_posix(),
                              {row["relative_path"] for row in selected["unresolved_required"]})

    def test_unsaved_native_result_does_not_waive_declared_hash_or_wrapper(self):
        folder = "data/research/captured/supplier_routes/acquisition-one/"
        missing = self.root / folder / "native_result.json"
        wrapper = self.root / folder / "wrapper.json"
        capture = self.json(folder + "capture.json", {"state": "failed", "native_result_saved": False,
            "native_reference": str(missing), "native_wrapper_reference": str(wrapper),
            "artifacts": {"native_result.json": "a" * 64}})
        self.state["source_checks"]["supplier-check"] = {"reference": str(capture)}
        selected = self.select()
        self.assertTrue({missing.relative_to(self.root).as_posix(), wrapper.relative_to(self.root).as_posix()}
                        <= {row["relative_path"] for row in selected["unresolved_required"]})
        self.json(folder + "native_result.json", {"late_original": True})
        selected = self.select()
        self.assertIn("BACKUP_SELECTION_HASH_MISMATCH", {row["reason"] for row in selected["unsafe"]})
        self.assertNotIn(missing.relative_to(self.root).as_posix(), self.files(selected))

    def test_late_original_native_result_is_preserved_even_when_original_flag_was_unsaved(self):
        native = self.json("data/research/captured/supplier_routes/native/request-one/native_result.json",
                           {"late_original": True})
        capture = self.json("data/research/captured/supplier_routes/acquisition-one/capture.json",
                            {"state": "failed", "native_result_saved": False, "native_reference": str(native)})
        self.state["source_checks"]["supplier-check"] = {"reference": str(capture)}
        selected = self.select()
        row = self.files(selected)[native.relative_to(self.root).as_posix()]
        self.assertEqual(row["expected_sha256"], sha256(native.read_bytes()).hexdigest())
        self.assertFalse(selected["unsafe"] or selected["unresolved_required"])

    def test_unsaved_native_result_keeps_relative_or_absolute_declared_artifact_hash(self):
        folder = "data/research/captured/supplier_routes/acquisition-one/"
        relative = folder + "native_result.json"
        native = self.json(relative, {"actual": "changed original fixture"})
        for proof_path in (relative, str(native)):
            with self.subTest(proof_path=proof_path):
                capture = self.json(folder + "capture.json", {"state": "failed", "native_result_saved": False,
                    "native_reference": str(native), "artifacts": {
                        "native_result.json": {"path": proof_path, "sha256": "a" * 64}}})
                self.state["source_checks"]["supplier-check"] = {"reference": str(capture)}
                selected = self.select()
                self.assertNotIn(relative, self.files(selected))
                self.assertIn("BACKUP_SELECTION_HASH_MISMATCH", {row["reason"] for row in selected["unsafe"]})

    def test_legacy_trends_csv_scope_and_original_hash_are_selected(self):
        csv = self.raw("media/live/trends/attempt-one/multiTimeline.csv", b"Week,Public query\n2026-01-01,10\n")
        scope = self.json("media/live/trends/attempt-one/scope.json", {"url": "https://trends.google.com"})
        receipt = self.json("media/live/trends/attempt-one/receipt.json", {"csv_path": str(csv), "csv_sha256": sha256(csv.read_bytes()).hexdigest(),
                            "artifacts": {"scope.json": sha256(scope.read_bytes()).hexdigest()}})
        self.state["source_checks"]["trends-check"] = {"reference": str(receipt)}
        self.assertEqual(len(self.select()["files"]), 3)

    def test_catalogue_seed_follows_material_page_and_original_marketplace(self):
        original, _ = self.capture()
        page = self.json("data/research/captured/catalogue_materials/pages/page-one/page.json",
                         {"receipt": {"origin": {"kind": "verified_marketplace", "receipt": str(original)}}})
        material = self.json("data/research/captured/catalogue_materials/items/material-one/material.json",
                             {"page_reference": str(page), "original_reference": str(original)})
        prepared = self.json("data/research/captured/catalogue_materials/preparations/branch-one.json", {"material_ids": ["material-one"]})
        self.state["batch_runs"]["seed-one"] = {"planned_branches": [{"prepared_reference": str(prepared), "material_ids": ["material-one"]}]}
        rows = self.files(self.select())
        self.assertEqual(len(rows), 4)
        self.assertIn(material.relative_to(self.root).as_posix(), rows)
        self.assertIn(original.relative_to(self.root).as_posix(), rows)

    def test_accepted_catalogue_material_is_selected_without_a_seed_cycle(self):
        material = self.json("data/research/captured/catalogue_materials/items/material-one/material.json", {"label": "Public type"})
        self.state["catalogue_additions"]["addition-one"] = {"material_id": "material-one"}
        self.assertIn(material.relative_to(self.root).as_posix(), self.files(self.select()))

    def test_photo_byte_hash_and_frozen_report_attachments(self):
        image = self.raw("media/live/product-images/photo-one.png", b"synthetic public photo")
        photo = {"path": str(image), "sha256": sha256(image.read_bytes()).hexdigest()}
        self.state["source_checks"]["photo-check"] = {"kind": "public_product_image", "state": "usable", "image": photo}
        pdf = self.raw("media/live/reports/issue-one/revision-1/report-en.pdf", b"synthetic frozen PDF")
        self.json("data/outputs/issue-one/revision-1/package-input.json", {"issue": {"issue_id": "issue-one"}, "product_facts": [{"image_provenance": photo}]})
        self.json("data/outputs/issue-one/revision-1/delivery-manifest.json", {"artifacts": {"pdf-en": {"path": str(pdf), "sha256": sha256(pdf.read_bytes()).hexdigest()}}})
        self.state["published_history"]["published-one"] = {"issue_id": "issue-one", "revision": 1}
        selected = self.select(); rows = self.files(selected)
        self.assertIn(pdf.relative_to(self.root).as_posix(), rows)
        self.assertEqual(rows[image.relative_to(self.root).as_posix()]["kind"], "public_image")
        self.assertEqual(len(rows[image.relative_to(self.root).as_posix()]["origins"]), 2)

    def test_interrupted_report_intent_payload_and_pdf_receipt_keep_original_bytes(self):
        self.programme()["drafts"]["issue-one"] = {"revision": 1, "complete": False}
        data = "data/outputs/issue-one/revision-1/internal-stages/"
        media = "media/live/reports/issue-one/revision-1/internal-stages/"
        payload = self.raw(media + "report-en.payload.html", b"<html>original saved edition</html>")
        html = self.raw(media + "report-en.html", payload.read_bytes())
        intent = self.json(data + "edition-en-intent.json", {"binding": {"issue_id": "issue-one", "revision": 1},
            "artifacts": {"html": {"path": str(html), "payload_path": str(payload),
                "sha256": sha256(payload.read_bytes()).hexdigest(), "bytes": len(payload.read_bytes())}}})
        pdf = self.raw(media + "report-en.pdf", b"original PDF before delivery manifest")
        render_intent = self.json(data + "pdf-intent-en.json", {"input_hash": "a" * 64,
            "html_sha256": sha256(html.read_bytes()).hexdigest(), "path": str(pdf)})
        render_receipt = self.json(data + "pdf-receipt-en.json", {"path": str(pdf), "pdf_sha256": sha256(pdf.read_bytes()).hexdigest()})
        unrelated = self.raw(media + "unrelated-notes.txt")
        selected = self.select(); rows = self.files(selected)
        for path in (payload, html, intent, pdf, render_intent, render_receipt):
            self.assertIn(path.relative_to(self.root).as_posix(), rows)
            self.assertEqual(rows[path.relative_to(self.root).as_posix()]["expected_sha256"], sha256(path.read_bytes()).hexdigest())
        self.assertNotIn(unrelated.relative_to(self.root).as_posix(), rows)
        self.assertFalse(selected["unsafe"])

    def test_changed_saved_report_payload_is_a_named_limitation(self):
        self.programme()["drafts"]["issue-one"] = {"revision": 1, "complete": False}
        payload = self.raw("media/live/reports/issue-one/revision-1/internal-stages/report-en.payload.html")
        self.json("data/outputs/issue-one/revision-1/internal-stages/edition-en-intent.json", {
            "binding": {}, "artifacts": {"html": {"path": str(payload.with_name("report-en.html")),
                "payload_path": str(payload), "sha256": "a" * 64, "bytes": len(payload.read_bytes())}}})
        selected = self.select()
        self.assertIn("BACKUP_SELECTION_HASH_MISMATCH", {row["reason"] for row in selected["unsafe"]})

    def test_saved_deadline_projection_keeps_original_receipt_and_snapshot(self):
        folder = "data/research/captured/direct_meta/acquisition-one/"
        original = self.json(folder + "receipt.json", {"state": "failed", "reason": "SOURCE_ACTION_TIMEOUT", "artifacts": {}})
        snapshot = self.raw(folder + "deadline_snapshot.json", b'{"text":"saved public cards"}')
        derived = self.json(folder + "deadline_reparse.json", {"state": "usable", "snapshot_file": "deadline_snapshot.json",
            "raw_hash": sha256(snapshot.read_bytes()).hexdigest(), "recovery": {"version": "source-deadline-reparse-v1",
                "original_receipt_sha256": sha256(original.read_bytes()).hexdigest(), "original_artifacts_hash": digest({})}})
        self.state["captures"]["capture-one"] = {"source": "meta", "reference": str(derived)}
        selected = self.select(); rows = self.files(selected)
        self.assertEqual(set(rows), {path.relative_to(self.root).as_posix() for path in (original, snapshot, derived)})
        self.assertFalse(selected["unsafe"])

    def test_changed_original_deadline_receipt_is_not_admitted(self):
        folder = "data/research/captured/direct_marketplace/acquisition-one/"
        original = self.json(folder + "receipt.json", {"state": "changed"})
        derived = self.json(folder + "deadline_reparse.json", {"state": "partial", "recovery": {
            "version": "source-deadline-reparse-v1", "original_receipt_sha256": "a" * 64, "original_artifacts_hash": digest({})}})
        self.state["captures"]["capture-one"] = {"source": "amazon", "reference": str(derived)}
        selected = self.select()
        self.assertNotIn(original.relative_to(self.root).as_posix(), self.files(selected))
        self.assertIn("BACKUP_SELECTION_HASH_MISMATCH", {row["reason"] for row in selected["unsafe"]})

    def test_malformed_saved_recovery_leaf_is_named_without_losing_sibling(self):
        for field in ("snapshot_file", "parsed_file", "scope_file", "interpretation_file"):
            with self.subTest(field=field):
                path, _ = self.capture(**{field: {"path": "not a permitted leaf"}},
                    recovery={"original_receipt_file": ["receipt.json"]})
                selected = self.select()
                self.assertIn(path.relative_to(self.root).as_posix(), self.files(selected))
                self.assertIn("BACKUP_SELECTION_ARTIFACT_NAME_INVALID", {row["reason"] for row in selected["unsafe"]})

    def test_supplier_dispatch_projection_follows_exact_original_and_native_dispatch(self):
        folder = "data/research/captured/supplier_routes/acquisition-one/"
        original = self.json(folder + "capture.json", {"records": [], "result": {"state": "failed"}})
        native = self.json("data/research/captured/supplier_routes/native/request-one/native_dispatch.json", {
            "navigation_started": True, "url": "https://example.com/public"})
        provenance = self.json(folder + "dispatch-recovery.json", {"version": "supplier-original-dispatch-recovery-v1",
            "original_reference": str(original), "original_sha256": sha256(original.read_bytes()).hexdigest(),
            "dispatch_proofs": {"request-one": {"dispatch_reference": str(native), "dispatch_sha256": sha256(native.read_bytes()).hexdigest()}}})
        derived = self.json(folder + "capture-dispatch-recovery.json", {"records": [], "result": {"state": "failed"}})
        self.state["supplier_assessments"]["supplier-one"] = {"reference": str(derived)}
        rows = self.files(self.select())
        self.assertEqual(set(rows), {path.relative_to(self.root).as_posix() for path in (original, native, provenance, derived)})

    def test_trends_evidence_original_csv_matches_saved_raw_hash(self):
        acquisition = "trends-one"
        folder = "media/live/trends/" + acquisition + "/"
        csv = self.raw(folder + "multiTimeline.csv", b"Month,public product\n2026-01,50\n")
        receipt = self.json(folder + "receipt.json", {"term": "public product", "csv_path": str(csv),
            "csv_sha256": sha256(csv.read_bytes()).hexdigest()})
        self.state["trends_evidence"]["evidence-one"] = {"acquisition_id": acquisition,
            "parsed": {"raw_sha256": sha256(csv.read_bytes()).hexdigest()}}
        selected = self.select(); rows = self.files(selected)
        self.assertEqual(set(rows), {path.relative_to(self.root).as_posix() for path in (receipt, csv)})
        self.assertFalse(selected["unsafe"] or selected["unresolved_required"])

    def test_trends_evidence_without_original_bytes_is_not_claimed_complete(self):
        self.state["trends_evidence"]["evidence-one"] = {"acquisition_id": "trends-one", "parsed": {"raw_sha256": "a" * 64}}
        selected = self.select()
        self.assertEqual(selected["files"], [])
        self.assertIn("BACKUP_SELECTION_TRENDS_RAW_REFERENCE_MISSING", {row["reason"] for row in selected["unresolved_required"]})

    def legacy_trends(self):
        folder = "media/live/trends/public-query-20261006/"
        csv = self.raw(folder + "multiTimeline.csv", b"Month,public query\n2026-01,50\n")
        metadata = {"term": "public query", "geo": "IN", "time_range": "today 12-m", "property": "web",
            "kind": "search_term", "collected_at": "2026-10-06T12:00:00+00:00",
            "source_reference": "https://trends.google.com/trends/explore?geo=IN&q=public%20query",
            "scope_verified": True}
        original = self.json(folder + "metadata.json", {**metadata, "acquisition_id": "public-query-20261006",
            "schema_version": 1, "route": "connected_tool_browser_csv", "csv_sha256": sha256(csv.read_bytes()).hexdigest()})
        browser = self.json(folder + "browser-capture.json", {"public_chart": "Original supervised chart capture."})
        self.state["trends_evidence"]["legacy-evidence"] = {"acquisition_id": "connected-csv-" + "a" * 40,
            "parsed": {"raw_sha256": sha256(csv.read_bytes()).hexdigest(), "metadata": metadata,
                "parser_version": "google-trends-csv-v1", "acquisition_route": "browser_csv"}}
        return csv, original, browser

    def test_legacy_trends_date_folder_uses_exact_csv_hash_and_saved_scope(self):
        originals = self.legacy_trends()
        with patch("urllib.request.urlopen", side_effect=AssertionError("Do not fetch missing originals")):
            selected = self.select()
        self.assertEqual(set(self.files(selected)), {path.relative_to(self.root).as_posix() for path in originals})
        self.assertFalse(selected["unsafe"] or selected["unresolved_required"])

    def test_legacy_trends_identical_csv_does_not_substitute_a_different_location(self):
        self.legacy_trends()
        self.state["trends_evidence"]["legacy-evidence"]["parsed"]["metadata"]["geo"] = "US"
        selected = self.select()
        self.assertEqual(selected["files"], [])
        self.assertIn("BACKUP_SELECTION_TRENDS_RAW_REFERENCE_MISSING", {row["reason"] for row in selected["unresolved_required"]})

    def test_legacy_trends_unverified_original_is_preserved_without_upgrading_its_scope(self):
        csv, original, browser = self.legacy_trends()
        metadata = self.state["trends_evidence"]["legacy-evidence"]["parsed"]["metadata"]
        metadata["scope_verified"] = False
        from product_radar.storage import load_json
        receipt = load_json(original); receipt["scope_verified"] = False
        atomic_json(original, receipt)
        selected = self.select()
        self.assertEqual(set(self.files(selected)), {path.relative_to(self.root).as_posix() for path in (csv, original, browser)})
        self.assertIs(metadata["scope_verified"], False)
        self.assertFalse(selected["unsafe"] or selected["unresolved_required"])

    def test_legacy_trends_changed_csv_stays_explicitly_unresolved(self):
        csv, _, _ = self.legacy_trends()
        atomic_bytes(csv, b"Changed export, not the saved original.\n")
        selected = self.select()
        self.assertNotIn(csv.relative_to(self.root).as_posix(), self.files(selected))
        self.assertIn("BACKUP_SELECTION_HASH_MISMATCH", {row["reason"] for row in selected["unsafe"]})
        self.assertIn("BACKUP_SELECTION_TRENDS_RAW_REFERENCE_MISSING", {row["reason"] for row in selected["unresolved_required"]})

    def test_published_revision_required_files_not_hidden_by_earlier_optional_selection(self):
        record = self.programme()
        record["drafts"]["issue-one"] = {"revision": 1, "complete": False}
        self.state["published_history"]["published-one"] = {"issue_id": "issue-one", "revision": 1}
        selected = self.select()
        self.assertTrue(any(row["relative_path"] and row["relative_path"].endswith("delivery-manifest.json")
                            for row in selected["unresolved_required"]))

    def test_pending_control_descriptor_hash_is_record_hash_not_wrapper_hash(self):
        record = self.programme()
        path = self.json("data/research/captured/programme_runs/" + key("programme", record["programme_id"]) + "/pending-control.json",
                         {"record": record, "record_hash": digest(record), "previous_hash": None})
        selected = self.select(pending_controls=[{"path": str(path), "record_hash": digest(record)}])
        self.assertEqual(selected["unsafe"], [])
        self.assertIn(path.relative_to(self.root).as_posix(), self.files(selected))

    def test_changed_pending_control_binding_is_not_adopted_as_trusted(self):
        record = self.programme(); changed = dict(record, concept_ids=["future-product"])
        self.json("data/research/captured/programme_runs/" + key("programme", record["programme_id"]) + "/pending-control.json",
                  {"record": changed, "record_hash": digest(changed), "previous_hash": digest(record)})
        selected = self.select()
        self.assertIn("BACKUP_SELECTION_PROGRAMME_CONTROL_INVALID", {row["reason"] for row in selected["unsafe"]})

    def test_public_code_assets_dependencies_and_settings_are_explicit(self):
        selected_paths = ["src/product_radar/one.py", "src/product_radar/providers/two.py", "scripts/run_programme.py",
                          "portal/worker.mjs", "requirements-research.txt", "docs/README.md",
                          "docs/product/india_dropshipping_product_opportunity_radar_product_brief.md",
                          "docs/operations/benchmarks/2026-10-03/benchmark_record.md",
                          "media/NeuraFlow/long_maroon_path_transparent_1024.png"]
        for path in selected_paths:
            self.raw(path)
        self.raw("scripts/dump_private_keys.py")
        self.raw("src/other_project/unrelated.py")
        self.raw("portal/worker.test.mjs")
        self.raw("README.md")  # Not this project's source-of-truth tracker.
        settings = self.json("config/operations/backup.json", {"schema_version": 1, "enabled": True, "destination": "data/backups", "timezone": "Asia/Kolkata"})
        rows = self.files(self.select())
        self.assertEqual(set(rows), set(selected_paths) | {settings.relative_to(self.root).as_posix()})
        self.assertEqual(rows[settings.relative_to(self.root).as_posix()]["kind"], "public_setting")
        with self.assertRaisesRegex(SafetyError, "PRIVATE_FILE"):
            _attachment({"path": settings, "kind": "source_capture"})  # Historical guard stays strict.

    def test_unknown_or_private_public_settings_fields_are_refused(self):
        for value in (None, [], {"schema_version": 1, "enabled": True, "destination": "data/backups", "timezone": "Asia/Kolkata", "secret": "x"}):
            with self.subTest(value=value):
                self.json("config/operations/backup.json", value)
                selected = self.select()
                self.assertEqual(selected["files"], [])
                self.assertTrue(selected["unsafe"])

    def test_ai_public_probe_timeout_is_optional_and_has_exact_checker_bounds(self):
        base = {"model": "gpt-6-luna", "reasoning_effort": "high", "authentication": "chatgpt", "paid_fallback": False}
        for extra in ({}, {"probe_timeout_seconds": 1}, {"probe_timeout_seconds": 45}, {"probe_timeout_seconds": 90}):
            with self.subTest(extra=extra):
                path = self.json("config/ai/codex_luna.json", {**base, **extra})
                selected = self.select()
                self.assertIn(path.relative_to(self.root).as_posix(), self.files(selected))
                self.assertFalse(selected["unsafe"])

    def test_ai_public_probe_timeout_rejects_invalid_types_bounds_and_other_fields(self):
        base = {"model": "gpt-6-luna", "reasoning_effort": "high", "authentication": "chatgpt", "paid_fallback": False}
        for extra in ([{"probe_timeout_seconds": value} for value in (True, False, None, 0, -1, 91, "45", 45.0)]
                      + [{"probe_timeout_seconds": 45, "other_setting": True}, {"probe_timeout_seconds": 45, "api_key": "not-real"}]):
            with self.subTest(extra=extra):
                self.json("config/ai/codex_luna.json", {**base, **extra})
                selected = self.select()
                self.assertEqual(selected["files"], [])
                self.assertTrue(selected["unsafe"])

    def test_confirmed_mismatch_repairs_do_not_hide_genuine_unresolved_references(self):
        self.json("config/ai/codex_luna.json", {"model": "gpt-6-luna", "reasoning_effort": "high",
            "authentication": "chatgpt", "paid_fallback": False, "probe_timeout_seconds": 45})
        proof = self.raw("data/research/captured/direct_marketplace/one/native_expression.js")
        self.capture(artifacts={proof.name: sha256(proof.read_bytes()).hexdigest(), "snapshot.json": "a" * 64})
        self.state["captures"]["capture-remote"] = {"source": "meta", "reference": "https://example.com/public"}
        self.state["trends_evidence"]["evidence-one"] = {"acquisition_id": "missing-trends", "parsed": {"raw_sha256": "a" * 64}}
        selected = self.select()
        reasons = {row["reason"] for row in selected["unresolved_required"]}
        self.assertTrue({"BACKUP_FILE_UNAVAILABLE", "BACKUP_SELECTION_NONLOCAL_REFERENCE", "BACKUP_SELECTION_TRENDS_RAW_REFERENCE_MISSING"} <= reasons)
        self.assertFalse(selected["unsafe"])

    def test_public_cost_baseline_preserves_exact_existing_selector_contract(self):
        baseline = {"schema_version": 1, "version": "historical-collection-baseline-v1", "baseline_id": "baseline-one",
            "observed_at": "2026-10-03T00:00:00Z", "baseline_collection_usd": "0.50", "baseline_candidates": 156,
            "reference": "docs/operations/build_activation_and_costs_2026-10-03.md#historical-cost", "source_mix": ["meta", "amazon"],
            "fit_policy_id": "fit-13-v1"}
        value = {"schema_version": 1, "selector": {"baseline_id": "baseline-one", "fingerprint": digest(baseline)}, "baseline": baseline}
        path = self.json("config/research/collection_cost_baseline.json", value)
        selected = self.select(); rows = self.files(selected)
        self.assertIn(path.relative_to(self.root).as_posix(), rows)
        self.assertFalse(selected["unsafe"])
        value["selector"]["fingerprint"] = "a" * 64
        atomic_json(path, value)
        selected = self.select()
        self.assertEqual(selected["files"], [])
        self.assertIn("COST_SNAPSHOT_BASELINE_SELECTOR_MISMATCH", {row["reason"] for row in selected["unsafe"]})

    def test_malformed_cost_baseline_is_local_and_healthy_code_still_selected(self):
        self.json("config/research/collection_cost_baseline.json", {"baseline": ["not a trusted baseline"]})
        code = self.raw("scripts/run_programme.py")
        selected = self.select()
        self.assertEqual(set(self.files(selected)), {code.relative_to(self.root).as_posix()})
        self.assertTrue(selected["unsafe"])

    def test_backup_settings_strict_local_destination(self):
        good = {"schema_version": 1, "enabled": True, "destination": "data/backups", "timezone": "Asia/Kolkata"}
        self.assertEqual(validate_backup_settings(good), good)
        for field, values in {"schema_version": [True, 2], "enabled": [1, "true"],
                              "destination": ["https://example.com", "/tmp/backups", "../backups", "config/local", "data/operations/backup_controls"],
                              "timezone": ["UTC"]}.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaisesRegex(SafetyError, "BACKUP_SETTINGS_INVALID"):
                    validate_backup_settings(dict(good, **{field: value}))

    def test_original_paid_public_identity_only_never_context_or_registry(self):
        attempt = "attempt-one"
        self.financial[1][attempt] = {"capture": None}
        self.json("data/benchmarks/state/apify/attempt-one/prepared.json", {"attempt_id": attempt})
        origin = self.json("config/local/apify_origins/attempt-one/origin.json", {
            "schema_version": 1, "attempt_id": attempt, "config_hash": "a" * 64,
            "account_id": "account-one", "namespace": "account-one:period-one", "generation": "generation-one"})
        self.json("config/local/apify_origins/attempt-one/context.json", {"profile": {"private": "excluded"}})
        self.json("config/local/apify_accounts.json", {"token": "excluded"})
        rows = self.files(self.select())
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[origin.relative_to(self.root).as_posix()]["kind"], "public_setting")

    def test_paid_http_activity_preserves_owned_response_and_hash(self):
        self.financial[1]["attempt-one"] = {"capture": None}
        self.json("data/benchmarks/state/apify/attempt-one/prepared.json", {"attempt_id": "attempt-one"})
        body = self.json("data/benchmarks/state/apify/attempt-one/http/response-one.body.json", {"data": {"id": "remote-one"}})
        self.json("data/benchmarks/state/apify/attempt-one/http/response-one.json", {"id": "response-one", "hash": sha256(body.read_bytes()).hexdigest()})
        self.json("data/benchmarks/state/apify/attempt-one/http_activity.json", [{"id": "response-one", "received_bytes": len(body.read_bytes())}])
        selected = self.select()
        self.assertEqual(len(selected["files"]), 4)
        self.assertEqual(selected["unsafe"], [])

    def test_pure_validators_work_after_original_files_removed(self):
        path, _ = self.capture()
        selected = self.select()
        path.unlink()
        with patch("product_radar.operational_backup_selection._read", side_effect=AssertionError("no original probes")), \
                patch.object(Path, "is_symlink", side_effect=AssertionError("no original probes")):
            self.assertEqual(validate_essential_backup_selection(selected, root=self.root), selected)
            self.assertEqual(validate_essential_backup_reference(selected["files"][0], root=self.root), selected["files"][0])

    def test_fingerprint_detects_changed_selection_and_resealed_unsafe_scope(self):
        self.capture(); selected = self.select(); changed = deepcopy(selected)
        changed["files"][0]["expected_sha256"] = "a" * 64
        with self.assertRaisesRegex(SafetyError, "FINGERPRINT"):
            validate_essential_backup_selection(changed, root=self.root)
        changed = deepcopy(selected)
        changed["files"][0].update(path=str(self.root / "config/local/auth.json"), relative_path="config/local/auth.json")
        changed["selection_fingerprint"] = digest({key: value for key, value in changed.items() if key != "selection_fingerprint"})
        with self.assertRaises(SafetyError):
            validate_essential_backup_selection(changed, root=self.root)

    def test_fingerprint_is_order_independent_for_supplied_record_maps(self):
        self.capture("one"); self.capture("two")
        first = self.select()
        self.state["captures"] = dict(reversed(list(self.state["captures"].items())))
        second = self.select()
        self.assertEqual(first["selection_fingerprint"], second["selection_fingerprint"])

    def test_file_and_total_caps_are_named_without_creating_a_copy(self):
        self.capture()
        with patch("product_radar.operational_backup_selection.MAX_BACKUP_BYTES", 1):
            selected = self.select()
        self.assertEqual(selected["files"], [])
        self.assertIn("BACKUP_SELECTION_TOTAL_SIZE_LIMIT", {row["reason"] for row in selected["unsafe"]})
        self.assertFalse((self.root / "data/backups").exists())

    def test_invalid_input_shape_has_fixed_error(self):
        with self.assertRaisesRegex(SafetyError, "BACKUP_SELECTION_INPUT_INVALID"):
            select_essential_backup_files({}, self.financial, root=self.root)
        with self.assertRaisesRegex(SafetyError, "BACKUP_SELECTION_INPUT_INVALID"):
            select_essential_backup_files(self.state, {}, root=self.root)


if __name__ == "__main__":
    unittest.main()
