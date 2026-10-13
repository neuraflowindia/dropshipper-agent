"""Relocatable working records: data/ and media/live/ only, failing closed.

Local temporary folders only; no provider, model, website or real store.
"""

import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path, PurePosixPath
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from product_radar import desktop_control as controls
from product_radar import runtime_storage
from product_radar.models import SafetyError
from product_radar.operational_backup_schedule import run_due_operational_backup
from product_radar.operational_backup_selection import _lexical_reference
from product_radar.operations import read_operation_status
from product_radar.programme import programme_service_state
from product_radar.runtime_storage import MARKER, MARKER_VALUE, logical_path, records_root, runtime_path, runtime_root


class RuntimeStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="radar-runtime-")
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(runtime_storage._configured.cache_clear)
        runtime_storage._configured.cache_clear()
        base = Path(self.temporary.name)
        self.project = base / "project"
        self.store = base / "fast-store"
        (self.project / "config/local").mkdir(parents=True)

    def relocate(self, *, marker=True, target=None):
        self.store.mkdir(parents=True, exist_ok=True)
        if marker:
            (self.store / MARKER).write_text(json.dumps(MARKER_VALUE))
        (self.project / "config/local/runtime_storage.json").write_text(json.dumps(
            {"schema_version": 1, "runtime_root": str(target or self.store)}))

    def test_default_keeps_every_tree_inside_the_project(self):
        self.assertEqual(runtime_root(self.project), self.project)
        self.assertEqual(runtime_path(self.project, "data/research/captured"), self.project / "data/research/captured")
        self.assertEqual(runtime_path(self.project, "media", "live", "reports"), self.project / "media/live/reports")
        self.assertEqual(logical_path(self.project, self.project / "data/outputs/x.json"), PurePosixPath("data/outputs/x.json"))
        self.assertIsNone(logical_path(self.project, self.store / "data/outputs/x.json"))

    def test_setting_moves_only_data_and_live_media(self):
        self.relocate()
        self.assertEqual(runtime_root(self.project), self.store)
        self.assertEqual(runtime_path(self.project, "data/research/captured"), self.store / "data/research/captured")
        self.assertEqual(runtime_path(self.project, "media/live/product-images"), self.store / "media/live/product-images")
        for kept in ("config/service.json", "media/NeuraFlow/logo.png", "src/product_radar/models.py",
                     "dataset/x.json", "media/livestock.png", "outputs/client-preview/a.pdf"):
            self.assertEqual(runtime_path(self.project, kept), self.project / kept)

    def test_logical_spelling_is_canonical_and_rejects_stale_project_copies(self):
        self.relocate()
        self.assertEqual(logical_path(self.project, self.store / "data/outputs/i/revision-1/a.json"),
                         PurePosixPath("data/outputs/i/revision-1/a.json"))
        self.assertEqual(logical_path(self.project, self.project / "config/service.json"), PurePosixPath("config/service.json"))
        self.assertIsNone(logical_path(self.project, self.project / "data/outputs/a.json"))
        self.assertIsNone(logical_path(self.project, self.project / "media/live/reports/a.pdf"))
        self.assertIsNone(logical_path(self.project, self.store / MARKER))
        self.assertIsNone(logical_path(self.project, Path("/elsewhere/data/a.json")))

    def test_missing_or_unmarked_folder_fails_closed_without_creating_a_store(self):
        self.relocate(marker=False)
        with self.assertRaisesRegex(SafetyError, "RUNTIME_STORAGE_UNAVAILABLE"):
            runtime_root(self.project)
        (self.store / MARKER).write_text(json.dumps({"schema_version": 1, "kind": "something-else"}))
        with self.assertRaisesRegex(SafetyError, "RUNTIME_STORAGE_UNAVAILABLE"):
            runtime_path(self.project, "data/research/captured")
        missing = self.store.parent / "never-created"
        self.relocate(target=missing)
        with self.assertRaisesRegex(SafetyError, "RUNTIME_STORAGE_UNAVAILABLE"):
            runtime_root(self.project)
        self.assertFalse(missing.exists())
        self.assertFalse((self.project / "data").exists())

    def test_invalid_settings_are_refused(self):
        setting = self.project / "config/local/runtime_storage.json"
        for value in ({"schema_version": 1, "runtime_root": "relative/store"},
                      {"schema_version": 1, "runtime_root": str(self.store) + "/../store"},
                      {"schema_version": 1, "runtime_root": str(self.project / "inside")},
                      {"schema_version": 1, "runtime_root": str(self.project)},
                      {"schema_version": 1, "runtime_root": str(self.project.parent)},
                      {"schema_version": 2, "runtime_root": str(self.store)},
                      {"schema_version": True, "runtime_root": str(self.store)},
                      {"schema_version": 1, "runtime_root": str(self.store), "extra": 1},
                      ["not", "an", "object"]):
            runtime_storage._configured.cache_clear()
            setting.write_text(json.dumps(value))
            with self.subTest(value=value), self.assertRaisesRegex(SafetyError, "RUNTIME_STORAGE_SETTING_INVALID"):
                runtime_root(self.project)

    def test_linked_store_is_refused(self):
        self.relocate()
        link = self.store.parent / "linked-store"
        os.symlink(self.store, link)
        self.relocate(target=link)
        with self.assertRaisesRegex(SafetyError, "SYMLINK_PATH_REFUSED"):
            runtime_root(self.project)

    def test_records_root_is_lexical_and_never_probes_files(self):
        with patch.object(Path, "exists", side_effect=AssertionError("File probe")), \
             patch("builtins.open", side_effect=AssertionError("File probe")):
            self.assertEqual(records_root(self.store / "data/research/captured", self.project), self.store)
            self.assertEqual(records_root(self.project / "research", self.project), self.project)

    def test_malformed_relative_paths_are_refused(self):
        for parts in (("",), ("../data",), ("/data",), ("data", ".."), ("data\\x",)):
            with self.subTest(parts=parts), self.assertRaisesRegex(SafetyError, "RUNTIME_PATH_INVALID"):
                runtime_path(self.project, *parts)

    def test_desktop_switch_and_activity_follow_the_relocated_store(self):
        self.relocate()
        with patch.object(controls, "owned_programmes", return_value=[]):
            self.assertEqual(controls.stop(self.project)["state"], "stopped")
        memory = SimpleNamespace(root=self.store / "data/research/captured")
        self.assertFalse(programme_service_state(memory)["enabled"])
        self.assertTrue((self.store / "data/operations/desktop").is_dir())
        self.assertFalse((self.project / "data").exists())
        current = self.store / "data/operations/research-runs/current.json"
        current.parent.mkdir(parents=True)
        current.write_text(json.dumps({"schema_version": 1, "run_id": "run-abc", "writer_active": False,
                                       "observed_at": "2026-10-13T00:00:00+00:00", "started_at": "2026-10-13T00:00:00+00:00"}))
        self.assertEqual(read_operation_status(self.project, as_of="2026-10-13T00:00:10+00:00")["state"], "saved_previous_run")

    def test_unavailable_store_never_reports_the_default_running_switch(self):
        self.relocate(marker=False)
        with patch.object(controls, "owned_programmes", return_value=[]):
            with self.assertRaisesRegex(SafetyError, "RUNTIME_STORAGE_UNAVAILABLE"):
                controls.status(self.project)
            with self.assertRaisesRegex(SafetyError, "RUNTIME_STORAGE_UNAVAILABLE"):
                controls.run_worker(self.project, ["--tick", "--execute"])
        self.assertFalse((self.project / "data").exists())

    def test_programme_activity_command_reads_the_relocated_store(self):
        self.relocate()
        import run_programme
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(run_programme.main(["--activity"], root=self.project), 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "not_recorded")

    def test_backup_references_use_logical_spelling(self):
        self.relocate()
        path, spelling = _lexical_reference("data/outputs/issue-a/revision-1/package-input.json", "report_package", self.project)
        self.assertEqual(path, self.store / "data/outputs/issue-a/revision-1/package-input.json")
        self.assertEqual(spelling, "data/outputs/issue-a/revision-1/package-input.json")
        path, spelling = _lexical_reference(str(self.store / "media/live/reports/i/revision-1/report-en.pdf"), "report_package", self.project)
        self.assertEqual(spelling, "media/live/reports/i/revision-1/report-en.pdf")
        path, spelling = _lexical_reference("config/service.json", "public_setting", self.project)
        self.assertEqual(path, self.project / "config/service.json")
        with self.assertRaisesRegex(SafetyError, "BACKUP_SELECTION_OUTSIDE_PROJECT"):
            _lexical_reference(str(self.project / "data/outputs/i/revision-1/package-input.json"), "report_package", self.project)

    def test_saved_backup_references_stay_checkable_after_the_store_folder_is_lost(self):
        from product_radar.operational_backup_selection import validate_essential_backup_reference
        self.relocate()
        path, spelling = _lexical_reference("data/outputs/i/revision-1/package-input.json", "report_package", self.project)
        reference = {"path": str(path), "relative_path": spelling, "kind": "report_package", "required": True,
                     "bytes": 1, "expected_sha256": "a" * 64, "expected_json_digest": None, "origins": ["issue/i"]}
        # The physical working-record folder disappears (for example a reset
        # Linux distribution); the project, its setting and data/backups remain.
        import shutil
        shutil.rmtree(self.store)
        runtime_storage._configured.cache_clear()
        with self.assertRaisesRegex(SafetyError, "RUNTIME_STORAGE_UNAVAILABLE"):
            runtime_path(self.project, "data/outputs")
        self.assertEqual(validate_essential_backup_reference(reference, root=self.project), reference)
        self.assertEqual(_lexical_reference(str(path), "report_package", self.project), (path, spelling))
        self.assertEqual(runtime_path(self.project, "data/outputs", lexical=True), self.store / "data/outputs")
        self.assertFalse(self.store.exists())
        # A stale in-project copy is still not the canonical record.
        with self.assertRaisesRegex(SafetyError, "BACKUP_SELECTION_OUTSIDE_PROJECT"):
            _lexical_reference(str(self.project / "data/outputs/i/revision-1/package-input.json"), "report_package", self.project)

    def test_store_overlapping_the_project_in_another_letter_case_is_refused(self):
        for target in (str(self.project).upper() + "/store", str(self.project.parent).swapcase()):
            runtime_storage._configured.cache_clear()
            (self.project / "config/local/runtime_storage.json").write_text(json.dumps(
                {"schema_version": 1, "runtime_root": target}))
            with self.subTest(target=target), self.assertRaisesRegex(SafetyError, "RUNTIME_STORAGE_SETTING_INVALID"):
                runtime_root(self.project, lexical=True)

    def test_daily_backup_lands_in_the_project_while_controls_stay_with_records(self):
        self.relocate()
        calls = []
        def creator(memory, destination, *, backup_id, root, pending_controls, max_parts):
            calls.append((destination, root))
            return {"manifest": None}
        memory = SimpleNamespace(root=self.store / "data/research/captured",
                                 journal=SimpleNamespace(root=self.store / "data/benchmarks/state/apify"))
        settings = {"backup_contract": "essential-operational-record-backup-v3", "destination": "data/backups",
                    "enabled": True, "schema_version": 2, "timezone": "Asia/Kolkata"}
        result = run_due_operational_backup(memory, root=self.project, now="2026-10-13T12:00:00+00:00",
                                            settings=settings, creator=creator)
        self.assertEqual(result["reason"], "LOCAL_BACKUP_PARTS_IN_PROGRESS")
        self.assertEqual(calls, [(self.project / "data/backups", self.project)])
        self.assertTrue((self.store / "data/operations/backup_controls/multipart-active.json").is_file())
        # A memory bound to the old in-project location is a binding change.
        stale = SimpleNamespace(root=self.project / "data/research/captured",
                                journal=SimpleNamespace(root=self.project / "data/benchmarks/state/apify"))
        self.assertEqual(run_due_operational_backup(stale, root=self.project, now="2026-10-14T12:00:00+00:00",
                         settings=settings, creator=creator)["reason"], "LOCAL_BACKUP_STORAGE_BINDING_CHANGED")


if __name__ == "__main__":
    unittest.main()
