"""Synthetic crash/continuation proofs, not client publication or live research."""

from copy import deepcopy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from product_radar import live_outputs as live
from product_radar.models import SafetyError, digest
from product_radar.storage import atomic_bytes, atomic_json, load_json
from test_live_outputs import fixture


class InternalReportStageTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.issue, self.dataset = fixture()
        self.data = self.root / "data/outputs" / self.issue["issue_id"] / "revision-1"
        self.stages = self.data / "internal-stages"
        self.media = self.root / "media/live/reports" / self.issue["issue_id"] / "revision-1"

    def test_checked_workbook_needs_no_writing_or_language_choice(self):
        self.dataset["narratives"] = {}
        receipt = live.write_issue_workbook(self.issue, self.dataset, self.root)
        self.assertTrue(receipt["complete"])
        self.assertFalse(receipt["approved"])
        self.assertFalse(receipt["release_ready"])
        core = load_json(self.stages / "checked-product-facts.json", live.MAX_CORE_FILE)
        self.assertNotIn("narratives", core)
        self.assertNotIn("selected_language", core)
        self.assertEqual(receipt["core_facts_hash"], digest(core))
        self.assertFalse((self.data / "final_report_data.json").exists())
        self.assertFalse((self.data / "delivery-manifest.json").exists())
        self.assertFalse(list(self.data.glob("manifest-*.json")))
        self.assertEqual(Path(receipt["artifacts"]["workbook"]["path"]).parent, self.stages)

    def test_later_writing_and_language_choice_do_not_rebuild_workbook(self):
        before = live.write_issue_workbook(self.issue, self.dataset, self.root)
        self.dataset["selected_language"] = "en"
        self.dataset["narratives"] = {}
        with patch.object(live, "_workbook_bytes", side_effect=AssertionError("no regeneration")):
            after = live.write_issue_workbook(self.issue, self.dataset, self.root)
        self.assertEqual(before, after)

    def test_deterministic_workbook_excludes_wall_clock_zip_and_properties(self):
        first = live._workbook_bytes(self.issue, self.dataset, deterministic=True)
        time.sleep(1.05)
        second = live._workbook_bytes(self.issue, self.dataset, deterministic=True)
        self.assertEqual(first, second)
        from zipfile import ZipFile
        from io import BytesIO
        with ZipFile(BytesIO(first)) as archive:
            self.assertTrue(all(row.date_time == (1980, 1, 1, 0, 0, 0) for row in archive.infolist()))
            self.assertIn((self.dataset["release_date"] + "T00:00:00Z").encode(), archive.read("docProps/core.xml"))

    def test_healthy_english_can_finish_with_malformed_unrelated_hinglish(self):
        self.dataset["narratives"]["hi-Latn"] = {"unfinished": True}
        edition = live.write_issue_edition(self.issue, self.dataset, self.root)
        self.assertTrue(edition["complete"])
        self.assertEqual(edition["language"], "en")
        self.assertEqual(edition["source_english_hash"], None)
        self.assertEqual(Path(edition["artifacts"]["html"]["path"]), self.media / "internal-stages/report-en.html")
        self.assertFalse((self.stages / "shortlist.xlsx").exists())
        self.assertFalse((self.data / "final_report_data.json").exists())

    def test_english_replay_does_not_render_again_when_other_language_finishes(self):
        partial = deepcopy(self.dataset)
        partial["narratives"].pop("hi-Latn")
        first = live.write_issue_edition(self.issue, partial, self.root)
        with patch.object(live, "render_html", side_effect=AssertionError("no rerender")):
            second = live.write_issue_edition(self.issue, self.dataset, self.root)
        self.assertEqual(first, second)
        self.assertEqual(first["artifacts"]["html"]["sha256"], live.digest_bytes(Path(first["artifacts"]["html"]["path"]).read_bytes()))

    def test_hinglish_requires_and_binds_actual_english(self):
        missing = deepcopy(self.dataset)
        missing["narratives"].pop("en")
        with self.assertRaisesRegex(SafetyError, "SOURCE_ENGLISH_REQUIRED"):
            live.write_issue_edition(self.issue, missing, self.root, "hi-Latn")
        first = live.write_issue_edition(self.issue, self.dataset, self.root, "hi-Latn")
        self.assertEqual(first["source_english_hash"], digest(self.dataset["narratives"]["en"]))
        self.dataset["narratives"]["en"]["title"] = "Different product picks"
        with self.assertRaisesRegex(SafetyError, "STAGE_BINDING_CHANGED"):
            live.write_issue_edition(self.issue, self.dataset, self.root, "hi-Latn")

    def test_changed_own_writing_does_not_overwrite_accepted_html(self):
        first = live.write_issue_edition(self.issue, self.dataset, self.root)
        path = Path(first["artifacts"]["html"]["path"])
        before = path.read_bytes()
        self.dataset["narratives"]["en"]["title"] = "Different product picks"
        with self.assertRaisesRegex(SafetyError, "STAGE_BINDING_CHANGED"):
            live.write_issue_edition(self.issue, self.dataset, self.root)
        self.assertEqual(path.read_bytes(), before)

    def test_changed_core_facts_require_new_revision_before_any_stage_overwrite(self):
        first = live.write_issue_workbook(self.issue, self.dataset, self.root)
        core = (self.stages / "checked-product-facts.json").read_bytes()
        self.dataset["products"][0]["prices"][0]["amount"] = "199"
        for writer in (live.write_issue_workbook, live.write_issue_edition):
            with self.subTest(writer=writer.__name__), self.assertRaisesRegex(SafetyError, "STAGE_BINDING_CHANGED"):
                writer(self.issue, self.dataset, self.root)
        self.assertEqual((self.stages / "checked-product-facts.json").read_bytes(), core)
        self.assertTrue(Path(first["artifacts"]["workbook"]["path"]).exists())

    def test_lost_workbook_alias_recovers_original_payload_without_regeneration(self):
        def fail_alias(path, raw):
            if Path(path).name == "shortlist.xlsx":
                raise OSError("synthetic interruption")
            atomic_bytes(path, raw)
        with patch.object(live, "atomic_bytes", side_effect=fail_alias), self.assertRaises(OSError):
            live.write_issue_workbook(self.issue, self.dataset, self.root)
        self.assertTrue((self.stages / "shortlist.payload.xlsx").exists())
        self.assertTrue((self.stages / "workbook-intent.json").exists())
        self.assertFalse((self.stages / "workbook-receipt.json").exists())
        original = (self.stages / "shortlist.payload.xlsx").read_bytes()
        with patch.object(live, "_workbook_bytes", side_effect=AssertionError("no regeneration")):
            receipt = live.write_issue_workbook(self.issue, self.dataset, self.root)
        self.assertEqual(Path(receipt["artifacts"]["workbook"]["path"]).read_bytes(), original)

    def test_pre_intent_interruption_rechecks_deterministic_original_payload(self):
        def fail_intent(path, value):
            if Path(path).name == "workbook-intent.json":
                raise OSError("synthetic interruption")
            atomic_json(path, value)
        with patch.object(live, "atomic_json", side_effect=fail_intent), self.assertRaises(OSError):
            live.write_issue_workbook(self.issue, self.dataset, self.root)
        raw = (self.stages / "shortlist.payload.xlsx").read_bytes()
        self.assertFalse((self.stages / "workbook-intent.json").exists())
        receipt = live.write_issue_workbook(self.issue, self.dataset, self.root)
        self.assertEqual(Path(receipt["artifacts"]["workbook"]["path"]).read_bytes(), raw)

    def test_lost_edition_receipt_reply_uses_original_exact_html(self):
        def lose_reply(path, value):
            atomic_json(path, value)
            if Path(path).name == "edition-en-receipt.json":
                raise OSError("synthetic lost reply")
        with patch.object(live, "atomic_json", side_effect=lose_reply), self.assertRaises(OSError):
            live.write_issue_edition(self.issue, self.dataset, self.root)
        with patch.object(live, "render_html", side_effect=AssertionError("no rerender")):
            receipt = live.write_issue_edition(self.issue, self.dataset, self.root)
        self.assertTrue(receipt["complete"])

    def test_workbook_failure_does_not_block_healthy_edition(self):
        with patch.object(live, "_workbook_bytes", side_effect=OSError("synthetic xlsx failure")), self.assertRaises(OSError):
            live.write_issue_workbook(self.issue, self.dataset, self.root)
        receipt = live.write_issue_edition(self.issue, self.dataset, self.root)
        self.assertTrue(Path(receipt["artifacts"]["html"]["path"]).exists())
        self.assertFalse((self.stages / "shortlist.xlsx").exists())

    def test_accepted_stage_artifact_or_payload_tampering_never_auto_repairs(self):
        for name in ("shortlist.xlsx", "shortlist.payload.xlsx"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as root:
                receipt = live.write_issue_workbook(self.issue, self.dataset, root)
                stages = Path(receipt["artifacts"]["workbook"]["path"]).parent
                path = stages / name
                atomic_bytes(path, path.read_bytes() + b"changed")
                with self.assertRaisesRegex(SafetyError, "STAGE_ARTIFACT_CHANGED"):
                    live.write_issue_workbook(self.issue, self.dataset, root)
                self.assertTrue(path.read_bytes().endswith(b"changed"))

    def test_completed_alias_disappearance_is_integrity_not_save_recovery(self):
        receipt = live.write_issue_edition(self.issue, self.dataset, self.root)
        Path(receipt["artifacts"]["html"]["path"]).unlink()
        with self.assertRaisesRegex(SafetyError, "STAGE_FILE_MISSING"):
            live.write_issue_edition(self.issue, self.dataset, self.root)

    def test_payload_alias_and_parent_symlinks_are_refused(self):
        for target in ("payload", "artifact", "parent"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as root:
                receipt = live.write_issue_workbook(self.issue, self.dataset, root)
                path = Path(receipt["artifacts"]["workbook"]["path"])
                if target == "payload":
                    path = path.with_name("shortlist.payload.xlsx")
                if target == "parent":
                    renamed = path.parent.with_name("original-stages")
                    path.parent.rename(renamed)
                    path.parent.symlink_to(renamed, target_is_directory=True)
                else:
                    raw = path.read_bytes()
                    path.unlink()
                    actual = path.with_name("synthetic-original")
                    atomic_bytes(actual, raw)
                    path.symlink_to(actual)
                with self.assertRaisesRegex(SafetyError, "SYMLINK_PATH_REFUSED"):
                    live.write_issue_workbook(self.issue, self.dataset, root)

    def test_stage_metadata_boolean_size_and_changed_contract_are_rejected(self):
        live.write_issue_workbook(self.issue, self.dataset, self.root)
        intent = load_json(self.stages / "workbook-intent.json", 128 * 1024)
        intent["artifacts"]["workbook"]["bytes"] = True
        atomic_json(self.stages / "workbook-intent.json", intent)
        with self.assertRaisesRegex(SafetyError, "STAGE_ARTIFACT_INVALID"):
            live.write_issue_workbook(self.issue, self.dataset, self.root)

    def test_boolean_numeric_aliases_cannot_change_a_checked_stage_contract(self):
        live.write_issue_workbook(self.issue, self.dataset, self.root)
        for field in ("receipt", "intent"):
            with self.subTest(field=field):
                path = self.stages / ("workbook-" + field + ".json")
                saved = load_json(path, 128 * 1024)
                changed = deepcopy(saved)
                if field == "receipt":
                    changed["complete"] = 1
                else:
                    changed["binding"]["revision"] = True
                atomic_json(path, changed)
                with self.assertRaisesRegex(SafetyError, "STAGE_(RECEIPT|BINDING)_CHANGED"):
                    live.write_issue_workbook(self.issue, self.dataset, self.root)
                atomic_json(path, saved)
        with patch.object(live, "WORKBOOK_GENERATION_VERSION", "different-version"), \
                self.assertRaisesRegex(SafetyError, "STAGE_BINDING_CHANGED"):
            live.write_issue_workbook(self.issue, self.dataset, self.root)

    def test_html_bytes_are_bounded_before_intent(self):
        with patch.object(live, "MAX_STAGE_FILE", 100), patch.object(live, "render_html", return_value="x" * 101), \
                self.assertRaisesRegex(SafetyError, "STAGE_FILE_SIZE_INVALID"):
            live.write_issue_edition(self.issue, self.dataset, self.root)
        self.assertFalse((self.stages / "edition-en-intent.json").exists())

    def test_partial_writing_cannot_freeze_final_facts(self):
        partial = deepcopy(self.dataset)
        partial["narratives"].pop("hi-Latn")
        live.write_issue_workbook(self.issue, partial, self.root)
        live.write_issue_edition(self.issue, partial, self.root)
        with self.assertRaisesRegex(SafetyError, "FINAL_WRITING_NOT_COMPLETE"):
            live.write_issue_package(self.issue, partial, self.root)
        self.assertFalse((self.data / "final_report_data.json").exists())
        self.assertFalse((self.data / "manifest-en.json").exists())

    def test_complete_writing_promotes_exact_checked_workbook_and_own_html(self):
        partial = deepcopy(self.dataset)
        partial["narratives"].pop("hi-Latn")
        workbook = live.write_issue_workbook(self.issue, partial, self.root)
        english = live.write_issue_edition(self.issue, partial, self.root)
        with patch.object(live, "_workbook_bytes", side_effect=AssertionError("no regeneration")), \
                patch.object(live, "render_html", side_effect=AssertionError("no rerender")):
            manifest = live.write_issue_package(self.issue, self.dataset, self.root)
        self.assertEqual(manifest["artifacts"]["workbook"]["sha256"], workbook["artifacts"]["workbook"]["sha256"])
        self.assertEqual(manifest["artifacts"]["html"]["sha256"], english["artifacts"]["html"]["sha256"])
        self.assertEqual(Path(manifest["artifacts"]["html"]["path"]).read_bytes(), Path(english["artifacts"]["html"]["path"]).read_bytes())
        live.validate_stage_provenance(self.dataset, "en", manifest["internal_stage"])
        hinglish = live.write_issue_package(self.issue, self.dataset, self.root, "hi-Latn")
        self.assertEqual(manifest["artifacts"]["workbook"], hinglish["artifacts"]["workbook"])
        self.assertEqual(manifest, live.write_issue_package(self.issue, self.dataset, self.root))

    def test_changed_interrupted_final_alias_is_not_overwritten(self):
        live.write_issue_workbook(self.issue, self.dataset, self.root)
        atomic_bytes(self.data / "shortlist.xlsx", b"not accepted workbook")
        with self.assertRaisesRegex(SafetyError, "PACKAGE_INTEGRITY_CHANGED"):
            live.write_issue_package(self.issue, self.dataset, self.root)
        self.assertEqual((self.data / "shortlist.xlsx").read_bytes(), b"not accepted workbook")
        self.assertFalse((self.data / "final_report_data.json").exists())

    def test_current_ten_point_workbook_and_provenance_are_checked(self):
        from test_fit_pipeline import current_fixture, selection
        _, issue, dataset = selection(current_fixture())
        receipt = live.write_issue_workbook(issue, dataset, self.root)
        self.assertEqual(receipt["workbook_contract"], "fit-checklist-v1")
        from product_radar.delivery_package import _workbook
        checked = deepcopy(dataset)
        checked["narratives"] = {}
        checked["selected_language"] = None
        _workbook(Path(receipt["artifacts"]["workbook"]["path"]).read_bytes(), checked)

    def test_verified_partial_legacy_draft_reuses_original_nondeterministic_workbook(self):
        first = live.write_issue_package(self.issue, self.dataset, self.root)
        original_workbook = Path(first["artifacts"]["workbook"]["path"]).read_bytes()
        original_facts = (self.data / "final_report_data.json").read_bytes()
        original_html = Path(first["artifacts"]["html"]["path"]).read_bytes()
        with patch.object(live, "_workbook_bytes", side_effect=AssertionError("no regeneration")):
            receipt = live.write_issue_workbook(self.issue, self.dataset, self.root)
            second = live.write_issue_package(self.issue, self.dataset, self.root, "hi-Latn")
            repeated = live.write_issue_workbook(self.issue, self.dataset, self.root)
        self.assertEqual(receipt, repeated)
        self.assertEqual(receipt["generation_version"], live.LEGACY_WORKBOOK_GENERATION_VERSION)
        self.assertEqual(receipt["legacy_origin"]["manifest_hash"], digest(first))
        self.assertEqual(Path(receipt["artifacts"]["workbook"]["path"]).read_bytes(), original_workbook)
        self.assertEqual(Path(second["artifacts"]["workbook"]["path"]).read_bytes(), original_workbook)
        self.assertEqual((self.data / "final_report_data.json").read_bytes(), original_facts)
        self.assertEqual(Path(first["artifacts"]["html"]["path"]).read_bytes(), original_html)
        live.validate_stage_provenance(self.dataset, "hi-Latn", second["internal_stage"])

    def test_unproven_existing_workbook_is_not_adopted_as_legacy(self):
        atomic_bytes(self.data / "shortlist.xlsx", b"foreign workbook")
        receipt = live.write_issue_workbook(self.issue, self.dataset, self.root)
        self.assertEqual(receipt["generation_version"], live.WORKBOOK_GENERATION_VERSION)
        self.assertNotIn("legacy_origin", receipt)
        self.assertNotEqual(Path(receipt["artifacts"]["workbook"]["path"]).read_bytes(), b"foreign workbook")

    def test_changed_legacy_workbook_or_manifest_cannot_be_adopted(self):
        for field in ("workbook", "manifest", "path"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as root:
                first = live.write_issue_package(self.issue, self.dataset, root)
                manifest_path = Path(root) / "data/outputs" / self.issue["issue_id"] / "revision-1/manifest-en.json"
                if field == "workbook":
                    atomic_bytes(Path(first["artifacts"]["workbook"]["path"]), b"changed")
                else:
                    changed = deepcopy(first)
                    if field == "manifest":
                        changed["selection_hash"] = "f" * 64
                    else:
                        changed["artifacts"]["workbook"]["path"] = str(Path(root) / "foreign.xlsx")
                    atomic_json(manifest_path, changed)
                with self.assertRaisesRegex(SafetyError, "PACKAGE_INTEGRITY_CHANGED"):
                    live.write_issue_workbook(self.issue, self.dataset, root)

    def test_old_facts_missing_hinglish_are_not_overwritten_to_finish_new_writing(self):
        partial = deepcopy(self.dataset)
        partial["narratives"].pop("hi-Latn")
        first = live.write_issue_package(self.issue, partial, self.root)
        raw = Path(first["artifacts"]["facts"]["path"]).read_bytes()
        live.write_issue_workbook(self.issue, self.dataset, self.root)
        live.write_issue_edition(self.issue, self.dataset, self.root, "hi-Latn")
        with self.assertRaisesRegex(SafetyError, "IMMUTABLE_FACTS_CHANGED"):
            live.write_issue_package(self.issue, self.dataset, self.root, "hi-Latn")
        self.assertEqual(Path(first["artifacts"]["facts"]["path"]).read_bytes(), raw)

    def test_final_stage_provenance_changes_are_rejected_on_replay(self):
        live.write_issue_workbook(self.issue, self.dataset, self.root)
        manifest = live.write_issue_package(self.issue, self.dataset, self.root)
        manifest["internal_stage"]["narrative_hash"] = "f" * 64
        atomic_json(self.data / "manifest-en.json", manifest)
        with self.assertRaisesRegex(SafetyError, "STAGE_PROVENANCE_CHANGED"):
            live.write_issue_package(self.issue, self.dataset, self.root)


if __name__ == "__main__":
    unittest.main()
