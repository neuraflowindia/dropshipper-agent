"""Connected report assembly using real files and simulated prose/browser."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.live_outputs import digest_bytes
from product_radar.delivery_package import write_delivery_manifest
from product_radar.issue_allocation import allocate_issues
from product_radar.models import SafetyError, digest
from product_radar.report_workflow import prepare_review_package
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, atomic_bytes, atomic_json, load_json
from test_live_outputs import fixture
from test_report_narration import NarrationRunner
from test_delivery_package import DeliveryPackageTests


def inspect(raw):
    with fitz.open(stream=raw, filetype="pdf") as doc:
        if any(p.rect.width >= p.rect.height or not p.get_text().strip() for p in doc):
            raise SafetyError("TEST_PDF_INVALID")
        return {"pages": len(doc), "portrait": True, "bytes": len(raw)}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.issue, dataset = fixture()
        self.facts = deepcopy(dataset["products"])
        self.runner, self.renders = NarrationRunner(memory=self.memory), []

    def render(self, html, pdf):
        self.renders.append(str(pdf))
        with fitz.open() as doc:
            page = doc.new_page(width=595, height=842)
            page.insert_text((40, 40), "Synthetic workflow PDF; no actual recommendations.")
            raw = doc.tobytes()
        atomic_bytes(pdf, raw)
        return dict(inspect(raw), path=str(pdf), html_sha256=digest_bytes(html.read_bytes()), pdf_sha256=digest_bytes(raw))

    def run_package(self, **options):
        return prepare_review_package(self.memory, self.issue, self.facts, root=self.root,
                                      runner=self.runner, render_pdf=self.render, inspect_pdf=inspect, **options)

    @property
    def data(self):
        return self.root / "data/outputs" / self.issue["issue_id"] / "revision-1"

    def test_complete_pair_connects_once_and_replays_without_model_or_browser(self):
        first = self.run_package()
        self.assertEqual(first["state"], "draft_ready_for_review")
        self.assertTrue(first["complete"])
        calls = len(self.runner.calls)
        self.assertEqual(len(self.renders), 2)
        self.assertFalse(first["approved"])
        self.assertFalse(first["client_ready"])
        saved_status = load_json(self.data / "package-status.json", 512 * 1024)
        preserved = {path: raw for path, raw in self.packet_bytes(1).items() if path.name != "package-status.json"}
        manifest = load_json(first["manifest_path"], 512 * 1024)
        self.assertEqual(manifest["languages"], ["en", "hi-Latn"])
        again = self.run_package(selected_language="en")
        self.assertTrue(again["reused"])
        self.assertEqual(again["package_hash"], first["package_hash"])
        self.assertEqual(len(self.runner.calls), calls)
        self.assertEqual(len(self.renders), 2)
        self.assertEqual(again["job_ids"], saved_status["job_ids"])
        self.assertEqual(again["stage_outcomes"], saved_status["stage_outcomes"])
        self.assertEqual(load_json(self.data / "package-status.json", 512 * 1024), again)
        self.assertTrue(all(path.read_bytes() == raw for path, raw in preserved.items()))
        third = self.run_package()
        self.assertEqual(third["job_ids"], first["job_ids"])
        self.assertEqual(third["stage_outcomes"], first["stage_outcomes"])
        self.assertEqual(len(self.runner.calls), calls)
        self.assertEqual(len(self.renders), 2)
        self.assertEqual(self.memory.snapshot()["issue_reservations"], {})
        self.assertEqual(self.memory.snapshot()["published_history"], {})

    def test_completed_replay_does_not_take_malformed_stale_or_foreign_status_metadata(self):
        first = self.run_package()
        path = self.data / "package-status.json"
        saved = load_json(path, 512 * 1024)
        calls, renders = len(self.runner.calls), len(self.renders)
        changes = {
            "foreign_issue": {"issue_id": "unrelated-issue"},
            "stale_revision": {"revision": 2},
            "bool_revision": {"revision": True},
            "stale_input": {"input_hash": "0" * 64},
            "foreign_package": {"package_hash": "0" * 64},
            "foreign_manifest": {"manifest_path": str(self.root / "other-manifest.json")},
            "incomplete": {"complete": False},
            "numeric_complete": {"complete": 1},
            "approved": {"approved": True},
            "numeric_approval": {"approved": 0},
            "client_ready": {"client_ready": True},
            "wrong_state": {"state": "render_pending"},
            "collector_calls": {"collector_requests": 1},
            "bool_collector_calls": {"collector_requests": False},
            "numeric_reused": {"reused": 1},
            "non_list_jobs": {"job_ids": "job-unrelated"},
            "invalid_jobs": {"job_ids": ["job-unrelated"]},
            "duplicate_jobs": {"job_ids": saved["job_ids"] * 2},
            "foreign_source_jobs": {"source_job_ids": saved["job_ids"]},
            "foreign_presentation": {"presentation_source": {"revision": 2}},
            "malformed_stages": {"stage_outcomes": []},
            "unknown_stages": {"stage_outcomes": {"other": {"state": "completed"}}},
        }
        for name, fields, value in (
                ("wrong_workbook_bytes", ("workbook", "receipt", "artifacts", "workbook", "sha256"), "0" * 64),
                ("foreign_workbook_receipt", ("workbook", "receipt", "issue_id"), "unrelated-issue"),
                ("unknown_workbook_receipt", ("workbook", "receipt", "foreign_detail"), "Unrelated metadata"),
                ("unknown_writing_field", ("writing", "foreign_detail"), "Unrelated metadata"),
                ("partial_writing", ("writing", "state"), "partial"),
                ("missing_written_language", ("writing", "complete_languages"), ["en"]),
                ("invalid_written_language", ("writing", "complete_languages"), [{}]),
                ("wrong_pdf_bytes", ("editions", "en", "pdf", "pdf_sha256"), "0" * 64),
                ("foreign_pdf_path", ("editions", "en", "pdf", "path"), str(self.root / "other.pdf")),
                ("foreign_html_path", ("editions", "en", "html", "path"), str(self.root / "other.html")),
                ("invalid_pdf_boolean", ("editions", "en", "pdf", "portrait"), 1),
                ("wrong_html_size", ("editions", "en", "html", "bytes"), 1),
                ("unknown_edition_field", ("editions", "en", "foreign_detail"), "Unrelated metadata")):
            stages = deepcopy(saved["stage_outcomes"])
            node = stages
            for field in fields[:-1]:
                node = node[field]
            node[fields[-1]] = value
            changes[name] = {"stage_outcomes": stages}
        for name, change in changes.items():
            with self.subTest(name=name):
                atomic_json(path, dict(deepcopy(saved), **change))
                replayed = self.run_package()
                self.assertEqual(replayed["package_hash"], first["package_hash"])
                self.assertTrue(replayed["complete"])
                self.assertFalse(replayed["approved"])
                self.assertFalse(replayed["client_ready"])
                self.assertNotIn("job_ids", replayed)
                self.assertNotIn("source_job_ids", replayed)
                self.assertNotIn("stage_outcomes", replayed)
        self.assertEqual(len(self.runner.calls), calls)
        self.assertEqual(len(self.renders), renders)

    def test_completed_replay_refuses_symlink_status_without_changing_its_target(self):
        self.run_package()
        path = self.data / "package-status.json"
        target = self.data / "saved-status.json"
        path.rename(target)
        raw = target.read_bytes()
        path.symlink_to(target)
        calls, renders = len(self.runner.calls), len(self.renders)
        with self.assertRaisesRegex(SafetyError, "SYMLINK_PATH_REFUSED"):
            self.run_package()
        self.assertTrue(path.is_symlink())
        self.assertEqual(target.read_bytes(), raw)
        self.assertEqual(len(self.runner.calls), calls)
        self.assertEqual(len(self.renders), renders)

    def test_completed_replay_uses_checked_packet_when_status_is_absent_or_invalid_json(self):
        first = self.run_package()
        path = self.data / "package-status.json"
        calls, renders = len(self.runner.calls), len(self.renders)
        for kind in ("absent", "invalid_json", "non_object"):
            with self.subTest(kind=kind):
                if kind == "absent":
                    path.unlink()
                elif kind == "invalid_json":
                    atomic_bytes(path, b'{"job_ids":')
                else:
                    atomic_json(path, ["foreign-status"])
                replayed = self.run_package()
                self.assertEqual(replayed["package_hash"], first["package_hash"])
                self.assertTrue(replayed["complete"])
                self.assertFalse(replayed["approved"])
                self.assertFalse(replayed["client_ready"])
                self.assertNotIn("job_ids", replayed)
                self.assertNotIn("stage_outcomes", replayed)
        self.assertEqual(len(self.runner.calls), calls)
        self.assertEqual(len(self.renders), renders)

    def test_completed_replay_retains_original_jobs_for_later_presentation_reuse(self):
        first = self.run_package()
        self.run_package()
        calls = len(self.runner.calls)
        self.advance_revision()
        with patch("product_radar.report_workflow.generate_report_narratives", side_effect=AssertionError("No model")):
            presented = self.run_package(presentation_source_revision=1)
            replayed = self.run_package(presentation_source_revision=1)
        self.assertEqual(presented["job_ids"], [])
        self.assertEqual(presented["source_job_ids"], first["job_ids"])
        self.assertEqual(replayed["source_job_ids"], first["job_ids"])
        self.assertEqual(replayed["stage_outcomes"], presented["stage_outcomes"])
        destination = self.data.parent / "revision-2/package-status.json"
        malformed = load_json(destination, 512 * 1024)
        malformed["presentation_source"]["revision"] = True
        atomic_json(destination, malformed)
        checked = self.run_package(presentation_source_revision=1)
        self.assertNotIn("stage_outcomes", checked)
        self.assertEqual(checked["source_job_ids"], first["job_ids"])
        self.assertEqual(len(self.runner.calls), calls)
        self.assertEqual(len(self.renders), 4)
        self.assertFalse(replayed["client_ready"])

    def test_completed_single_language_replay_keeps_internal_english_attribution(self):
        self.issue.update(projected_ordinal=3, language_state="language_pending")
        first = self.run_package(selected_language="hi-Latn")
        calls = len(self.runner.calls)
        replayed = self.run_package(selected_language="hi-Latn")
        self.assertEqual(replayed["job_ids"], first["job_ids"])
        self.assertEqual(replayed["stage_outcomes"], first["stage_outcomes"])
        self.assertEqual(set(replayed["stage_outcomes"]["editions"]), {"hi-Latn"})
        self.assertEqual(replayed["stage_outcomes"]["writing"]["complete_languages"], ["en", "hi-Latn"])
        self.assertEqual(len(self.runner.calls), calls)
        self.assertEqual(len(self.renders), 1)
        self.assertFalse(replayed["approved"])
        self.assertFalse(replayed["client_ready"])

    def test_short_issue_never_calls_model_or_renders(self):
        self.issue.update(ready_for_review=False, shortfall=3)
        result = self.run_package()
        self.assertEqual(result["state"], "shortfall")
        self.assertEqual(result["shortfall"], 3)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.renders, [])

    def test_later_language_pause_preserves_facts_then_finishes_chosen_edition(self):
        self.issue.update(projected_ordinal=3, language_state="language_pending")
        first = self.run_package()
        saved = (self.data / "package-input.json").read_bytes()
        self.assertEqual(first["state"], "language_pending")
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.renders, [])
        self.assertEqual(first["stage_outcomes"]["workbook"]["state"], "completed")
        self.assertTrue((self.data / "internal-stages/shortlist.xlsx").is_file())
        self.assertTrue((self.data / "internal-stages/checked-product-facts.json").is_file())
        self.assertFalse((self.data / "final_report_data.json").exists())
        final = self.run_package(selected_language="en")
        self.assertEqual(final["state"], "draft_ready_for_review")
        self.assertEqual(load_json(final["manifest_path"], 512 * 1024)["languages"], ["en"])
        self.assertEqual(len(self.renders), 1)
        self.assertEqual((self.data / "package-input.json").read_bytes(), saved)

    def test_later_hinglish_uses_internal_english_but_delivers_only_chosen_pdf(self):
        self.issue.update(projected_ordinal=3, language_state="language_pending")
        result = self.run_package(selected_language="hi-Latn")
        manifest = load_json(result["manifest_path"], 512 * 1024)
        facts = load_json(self.data / "final_report_data.json", 4 * 1024 * 1024)
        self.assertEqual(manifest["languages"], ["hi-Latn"])
        self.assertEqual(set(facts["narratives"]), {"en", "hi-Latn"})
        self.assertEqual(len(self.renders), 1)
        self.assertTrue(self.renders[0].endswith("report-hi-Latn.pdf"))
        self.assertEqual([f["name"] for f in manifest["files"]], ["shortlist.xlsx", "report-hi-Latn.pdf"])
        self.assertEqual(manifest["evidence_type"], "synthetic")
        self.assertFalse(result["approved"])
        self.assertFalse(result["client_ready"])

    def test_second_edition_render_failure_reuses_first_pdf_and_saved_prose(self):
        original = self.render
        calls = 0
        def partial(html, pdf):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise SafetyError("SIMULATED_SECOND_EDITION_FAILED")
            return original(html, pdf)
        with patch.object(self, "render", side_effect=partial):
            waiting = self.run_package()
        self.assertEqual(waiting["state"], "render_pending")
        self.assertEqual(waiting["stage_outcomes"]["editions"]["hi-Latn"]["reason"], "SIMULATED_SECOND_EDITION_FAILED")
        self.assertFalse((self.data / "delivery-manifest.json").exists())
        self.assertEqual(len(self.renders), 1)
        receipt = (self.data / "pdf-receipt-en.json").read_bytes()
        model_calls = len(self.runner.calls)
        result = self.run_package()
        self.assertEqual(result["state"], "draft_ready_for_review")
        self.assertEqual(len(self.renders), 2)
        self.assertEqual(len(self.runner.calls), model_calls)
        self.assertEqual((self.data / "pdf-receipt-en.json").read_bytes(), receipt)

    def test_changed_facts_require_new_revision_without_more_calls(self):
        self.run_package()
        count = len(self.runner.calls)
        self.facts[0]["title"] = "Changed product facts"
        with self.assertRaisesRegex(SafetyError, "FROZEN_INPUT_CHANGED"):
            self.run_package()
        self.assertEqual(len(self.runner.calls), count)

    def test_bad_prose_retains_partial_jobs_but_does_not_render(self):
        def break_one(item, value):
            if item["kind"] == "product":
                value["reason"] = "Unprotected numerical claim 999."
        self.runner = NarrationRunner(break_one)
        result = self.run_package()
        self.assertEqual(result["state"], "narration_pending")
        self.assertTrue(result["failures"])
        self.assertEqual(self.renders, [])
        self.assertEqual(result["stage_outcomes"]["workbook"]["state"], "completed")
        self.assertTrue((self.data / "internal-stages/shortlist.xlsx").is_file())
        self.assertFalse((self.data / "delivery-manifest.json").exists())

    def test_missing_receipt_after_owned_render_reuses_actual_pdf(self):
        original = atomic_json
        failed = False
        def save(path, value):
            nonlocal failed
            if Path(path).name == "pdf-receipt-en.json" and not failed:
                failed = True
                raise OSError("Simulated interruption after owned PDF creation")
            return original(path, value)
        with patch("product_radar.report_workflow.atomic_json", side_effect=save):
            waiting = self.run_package()
        self.assertEqual(waiting["state"], "render_pending")
        self.assertEqual(len(self.renders), 2)
        calls = len(self.runner.calls)
        result = self.run_package()
        self.assertEqual(result["state"], "draft_ready_for_review")
        self.assertEqual(len(self.renders), 2)
        self.assertEqual(len(self.runner.calls), calls)

    def test_existing_foreign_pdf_cannot_gain_a_receipt(self):
        from product_radar.live_outputs import build_issue_dataset, write_issue_package
        dataset = build_issue_dataset(self.issue, self.facts, evidence_type="synthetic")
        from product_radar.report_narration import generate_report_narratives
        dataset = generate_report_narratives(self.memory, self.issue, dataset, self.runner)["dataset"]
        draft = write_issue_package(self.issue, dataset, self.root)
        pdf = Path(draft["artifacts"]["html"]["path"]).with_suffix(".pdf")
        self.render(Path(draft["artifacts"]["html"]["path"]), pdf)
        waiting = self.run_package()
        self.assertEqual(waiting["state"], "render_pending")
        self.assertEqual(waiting["stage_outcomes"]["editions"]["en"]["reason"], "REPORT_RENDER_PROVENANCE_REQUIRED")
        self.assertFalse((self.data / "pdf-receipt-en.json").exists())
        self.assertFalse((self.data / "delivery-manifest.json").exists())

    def test_modified_final_pdf_is_detected_before_reuse(self):
        first = self.run_package()
        manifest = load_json(first["manifest_path"], 512 * 1024)
        path = Path(manifest["pdf_receipts"]["en"]["path"])
        atomic_bytes(path, path.read_bytes() + b"changed")
        with self.assertRaises(SafetyError):
            self.run_package()

    def test_failed_renderer_does_not_commit_delivery_manifest(self):
        with patch.object(self, "render", side_effect=SafetyError("NATIVE_RENDER_UNAVAILABLE")):
            result = self.run_package()
        self.assertEqual(result["state"], "render_pending")
        self.assertEqual({row["reason"] for row in result["stage_outcomes"]["editions"].values()}, {"NATIVE_RENDER_UNAVAILABLE"})
        self.assertEqual(result["stage_outcomes"]["workbook"]["state"], "completed")
        self.assertFalse((self.data / "delivery-manifest.json").exists())

    def advance_revision(self):
        self.issue["revision"] += 1
        self.issue["selection_hash"] = digest({key: self.issue[key] for key in (
            "issue_id", "client_id", "revision", "date", "products", "evidence_fingerprint")})

    def packet_bytes(self, revision):
        folders = (self.data.parent / ("revision-" + str(revision)),
                   self.root / "media/live/reports" / self.issue["issue_id"] / ("revision-" + str(revision)))
        # Lock diagnostics describe each attempt; they are not frozen packet
        # artifacts. Keep every other source, receipt and rendered file checked.
        return {path: path.read_bytes() for folder in folders for path in folder.rglob("*")
                if path.is_file() and path.relative_to(folder).parts[0] != "package_writer"}

    def source_bytes(self):
        return self.packet_bytes(1)

    def test_presentation_revision_copies_exact_writing_without_model_and_preserves_source(self):
        source = self.run_package()
        dataset = load_json(self.data / "final_report_data.json", 4 * 1024 * 1024)
        before = self.source_bytes()
        calls = len(self.runner.calls)
        self.advance_revision()
        with patch("product_radar.report_workflow.generate_report_narratives", side_effect=AssertionError("No model")):
            result = self.run_package(presentation_source_revision=1)
            again = self.run_package(presentation_source_revision=1, selected_language="en")
        self.assertTrue(result["complete"])
        self.assertTrue(again["reused"])
        self.assertEqual(result["job_ids"], [])
        self.assertEqual(result["source_job_ids"], source["job_ids"])
        self.assertEqual(len(self.runner.calls), calls)
        self.assertEqual(len(self.renders), 4)
        self.assertEqual(self.source_bytes(), before)
        destination = self.data.parent / "revision-2"
        saved = load_json(destination / "final_report_data.json", 4 * 1024 * 1024)
        self.assertEqual(saved["narratives"], dataset["narratives"])
        self.assertEqual(saved["products"], dataset["products"])
        self.assertEqual(saved["revision"], 2)
        binding = load_json(destination / "package-input.json", 8 * 1024 * 1024)
        self.assertEqual(binding["presentation_source"]["package_hash"], source["package_hash"])
        self.assertEqual(binding["presentation_source"]["facts_hash"], digest(dataset))
        self.assertEqual(binding["presentation_source"]["product_facts_hash"], digest(self.facts))
        self.assertFalse(result["approved"])
        self.assertFalse(result["client_ready"])
        self.assertEqual(self.memory.snapshot()["published_history"], {})

    def test_presentation_actual_allocator_revision_preserves_both_provenance_hashes_without_model(self):
        eligible = deepcopy(self.issue["products"])
        date = {key: self.issue[key] for key in ("issue_id", "client_id", "date")}
        first = allocate_issues(eligible, [dict(date, revision=1)], [], [])["issues"][0]
        second = allocate_issues(eligible, [dict(date, revision=2)], [], [])["issues"][0]
        self.assertNotEqual(first["input_hash"], second["input_hash"])
        ignored = {"revision", "selection_hash", "input_hash"}
        self.assertEqual({k: v for k, v in first.items() if k not in ignored},
                         {k: v for k, v in second.items() if k not in ignored})
        self.issue = first
        self.run_package()
        before = self.source_bytes()
        original_calls = len(self.runner.calls)
        self.issue = second
        with patch("product_radar.report_workflow.generate_report_narratives", side_effect=AssertionError("No model")):
            result = self.run_package(presentation_source_revision=1)
        self.assertTrue(result["complete"])
        self.assertEqual(len(self.runner.calls), original_calls)
        self.assertEqual(self.source_bytes(), before)
        frozen = load_json(self.data.parent / "revision-2/package-input.json", 8 * 1024 * 1024)
        self.assertEqual(frozen["issue"], second)
        self.assertEqual(load_json(self.data / "package-input.json", 8 * 1024 * 1024)["issue"], first)
        self.assertEqual(frozen["presentation_source"]["allocation_input_hash"], first["input_hash"])
        self.assertEqual(frozen["product_facts"], self.facts)

    def test_presentation_reuses_complete_legacy_raw_quote_source_but_destination_is_current(self):
        self.facts[0]["prices"][0].update(amount=None, raw_displayed="₹149–₹189", quantity=None,
                                           unit="not_stated", conditions="Displayed quote basis not verified.")
        original = self.run_package()
        accepted = load_json(self.data / "final_report_data.json", 4 * 1024 * 1024)
        source_input = load_json(self.data / "package-input.json", 8 * 1024 * 1024)
        # Reconstruct a historical rendering fixture from genuine accepted
        # fixture G slots, using the exact old deterministic formatter. This
        # creates no manufactured G response and does not overwrite the source.
        legacy_root = self.root / "legacy-render-fixture"
        with patch("test_delivery_package.fixture", return_value=(deepcopy(self.issue), deepcopy(accepted))):
            issue, dataset, drafts, receipts = DeliveryPackageTests().legacy_packet(legacy_root)
        self.assertEqual(dataset, accepted)
        manifest = write_delivery_manifest(issue, drafts, receipts, root=legacy_root)
        source_data = legacy_root / "data/outputs" / issue["issue_id"] / "revision-1"
        atomic_json(source_data / "package-input.json", source_input)
        atomic_json(source_data / "package-status.json", dict(original, package_hash=manifest["package_hash"],
                                                              manifest_path=str(source_data / "delivery-manifest.json")))
        before = {path: path.read_bytes() for path in legacy_root.rglob("*") if path.is_file()}
        calls = len(self.runner.calls)
        self.advance_revision()
        with patch("product_radar.report_workflow.generate_report_narratives", side_effect=AssertionError("No model")):
            result = prepare_review_package(self.memory, self.issue, self.facts, root=legacy_root,
                                            presentation_source_revision=1, render_pdf=self.render, inspect_pdf=inspect)
        self.assertTrue(result["complete"])
        self.assertEqual(len(self.runner.calls), calls)
        self.assertTrue(all(path.read_bytes() == raw for path, raw in before.items()))
        destination = load_json(result["manifest_path"], 512 * 1024)
        self.assertTrue(all(draft["price_display_version"] == "price-display-v2"
                            for draft in destination["language_manifests"].values()))
        self.assertTrue(all("price_display_version" not in draft for draft in manifest["language_manifests"].values()))
        new_dataset = load_json(source_data.parent / "revision-2/final_report_data.json", 4 * 1024 * 1024)
        self.assertEqual(new_dataset["products"], accepted["products"])
        self.assertEqual(new_dataset["narratives"], accepted["narratives"])

    def test_presentation_requires_present_valid_allocator_provenance_hash(self):
        self.run_package()
        self.advance_revision()
        for value in (None, "invalid", "G" * 64, 1):
            with self.subTest(value=value):
                self.issue["input_hash"] = value
                with self.assertRaisesRegex(SafetyError, "PRESENTATION_ALLOCATION_HASH_INVALID"):
                    self.run_package(presentation_source_revision=1)
                self.assertFalse((self.data.parent / "revision-2/package-input.json").exists())

    def test_presentation_partial_render_recovers_without_model_or_source_changes(self):
        self.run_package()
        before = self.source_bytes()
        self.advance_revision()
        original = self.render
        renders = 0
        def partial(html, pdf):
            nonlocal renders
            renders += 1
            if renders == 2:
                raise SafetyError("SIMULATED_PRESENTATION_RENDER_FAILED")
            return original(html, pdf)
        with patch("product_radar.report_workflow.generate_report_narratives", side_effect=AssertionError("No model")):
            with patch.object(self, "render", side_effect=partial):
                waiting = self.run_package(presentation_source_revision=1)
                self.assertEqual(waiting["state"], "render_pending")
                self.assertEqual(waiting["stage_outcomes"]["editions"]["hi-Latn"]["reason"], "SIMULATED_PRESENTATION_RENDER_FAILED")
            destination = self.data.parent / "revision-2"
            receipt = (destination / "pdf-receipt-en.json").read_bytes()
            frozen = (destination / "package-input.json").read_bytes()
            result = self.run_package(presentation_source_revision=1)
        self.assertTrue(result["complete"])
        self.assertEqual(len(self.renders), 4)
        self.assertEqual((destination / "pdf-receipt-en.json").read_bytes(), receipt)
        self.assertEqual((destination / "package-input.json").read_bytes(), frozen)
        self.assertEqual(self.source_bytes(), before)

    def test_first_render_timeout_keeps_healthy_second_edition_and_reuses_it(self):
        original = self.render
        calls = 0
        def first_failed(html, pdf):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError("Synthetic owned browser timeout")
            return original(html, pdf)
        with patch.object(self, "render", side_effect=first_failed):
            waiting = self.run_package()
        outcomes = waiting["stage_outcomes"]["editions"]
        self.assertEqual(outcomes["en"]["state"], "waiting")
        self.assertEqual(outcomes["en"]["error_type"], "TimeoutError")
        self.assertEqual(outcomes["hi-Latn"]["state"], "completed")
        retained = Path(outcomes["hi-Latn"]["pdf"]["path"]).read_bytes()
        model_calls = len(self.runner.calls)
        final = self.run_package()
        self.assertTrue(final["complete"])
        self.assertEqual(len(self.renders), 2)
        self.assertEqual(len(self.runner.calls), model_calls)
        self.assertEqual(Path(outcomes["hi-Latn"]["pdf"]["path"]).read_bytes(), retained)

    def test_completed_english_renders_before_hinglish_writing_then_promotes_without_rerender(self):
        from product_radar.report_narration import generate_report_narratives
        def english_only(memory, issue, dataset, runner, **options):
            result = generate_report_narratives(memory, issue, dataset, runner, languages=("en",))
            result["failures"] = {"hi-Latn": {"edition": "SIMULATED_WRITING_OUTAGE"}}
            return result
        with patch("product_radar.report_workflow.generate_report_narratives", side_effect=english_only):
            waiting = self.run_package()
        self.assertEqual(waiting["state"], "narration_pending")
        self.assertEqual(waiting["stage_outcomes"]["editions"]["en"]["state"], "completed")
        self.assertEqual(waiting["stage_outcomes"]["editions"]["hi-Latn"]["state"], "waiting")
        self.assertEqual(len(self.renders), 1)
        receipt = waiting["stage_outcomes"]["editions"]["en"]["pdf"]
        raw = Path(receipt["path"]).read_bytes()
        workbook = (self.data / "internal-stages/shortlist.xlsx").read_bytes()
        self.assertFalse((self.data / "final_report_data.json").exists())
        self.assertFalse((self.data / "delivery-manifest.json").exists())
        final = self.run_package()
        self.assertTrue(final["complete"])
        self.assertEqual(len(self.renders), 2)
        manifest = load_json(final["manifest_path"], 512 * 1024)
        self.assertEqual(Path(manifest["pdf_receipts"]["en"]["path"]).read_bytes(), raw)
        self.assertEqual((self.data / "shortlist.xlsx").read_bytes(), workbook)

    def test_failed_workbook_save_still_produces_both_completed_edition_stages(self):
        with patch("product_radar.report_workflow.write_issue_workbook", side_effect=OSError("Synthetic full disk")):
            waiting = self.run_package()
        self.assertEqual(waiting["state"], "render_pending")
        self.assertEqual(waiting["stage_outcomes"]["workbook"]["state"], "waiting")
        self.assertEqual({row["state"] for row in waiting["stage_outcomes"]["editions"].values()}, {"completed"})
        self.assertEqual(len(self.renders), 2)
        self.assertFalse((self.data / "delivery-manifest.json").exists())
        calls = len(self.runner.calls)
        final = self.run_package()
        self.assertTrue(final["complete"])
        self.assertEqual(len(self.renders), 2)
        self.assertEqual(len(self.runner.calls), calls)

    def test_missing_native_renderer_keeps_checked_workbook_and_both_html_editions(self):
        waiting = prepare_review_package(self.memory, self.issue, self.facts, root=self.root, runner=self.runner)
        self.assertEqual(waiting["state"], "render_pending")
        self.assertEqual(waiting["stage_outcomes"]["workbook"]["state"], "completed")
        for row in waiting["stage_outcomes"]["editions"].values():
            self.assertTrue(Path(row["html"]["path"]).is_file())
            self.assertEqual(row["reason"], "REPORT_NATIVE_RENDERER_REQUIRED")
        self.assertEqual(self.renders, [])
        self.assertFalse((self.data / "delivery-manifest.json").exists())

    def test_earlier_owned_first_edition_recovers_second_without_changing_original_bytes(self):
        from product_radar.live_outputs import build_issue_dataset, write_issue_package
        from product_radar.report_narration import generate_report_narratives
        dataset = build_issue_dataset(self.issue, self.facts, evidence_type="synthetic")
        dataset = generate_report_narratives(self.memory, self.issue, dataset, self.runner)["dataset"]
        draft = write_issue_package(self.issue, dataset, self.root)
        binding = {"schema_version": 1, "issue": deepcopy(self.issue), "product_facts": deepcopy(self.facts),
                   "evidence_type": "synthetic"}
        atomic_json(self.data / "package-input.json", binding)
        html = Path(draft["artifacts"]["html"]["path"])
        pdf = html.with_suffix(".pdf")
        atomic_json(self.data / "pdf-intent-en.json", {"input_hash": digest(binding),
                    "html_sha256": draft["artifacts"]["html"]["sha256"], "path": str(pdf)})
        receipt = self.render(html, pdf)
        atomic_json(self.data / "pdf-receipt-en.json", receipt)
        retained = {path: path.read_bytes() for path in (
            html, pdf, self.data / "shortlist.xlsx", self.data / "final_report_data.json",
            self.data / "manifest-en.json", self.data / "pdf-intent-en.json", self.data / "pdf-receipt-en.json")}
        calls = len(self.runner.calls)
        final = self.run_package()
        self.assertTrue(final["complete"])
        self.assertEqual(len(self.renders), 2)
        self.assertEqual(len(self.runner.calls), calls)
        self.assertTrue(all(path.read_bytes() == raw for path, raw in retained.items()))

    def test_presentation_rejects_changed_date_client_selection_evidence_and_facts(self):
        self.run_package()
        before = self.source_bytes()
        self.advance_revision()
        issue, facts = deepcopy(self.issue), deepcopy(self.facts)
        for kind in ("date", "client_id", "evidence_fingerprint", "projected_ordinal", "selection", "facts", "evidence_type"):
            with self.subTest(kind=kind):
                self.issue, self.facts = deepcopy(issue), deepcopy(facts)
                if kind == "facts":
                    self.facts[0]["title"] = "Changed source facts"
                elif kind == "evidence_type":
                    self.memory.evidence_type = "captured"
                elif kind == "projected_ordinal":
                    self.issue[kind] = 2
                elif kind == "selection":
                    self.issue["products"][0]["kpi_states"][8] = "Meets"
                else:
                    self.issue[kind] = "2026-10-08" if kind == "date" else "other-client" if kind == "client_id" else "b" * 64
                self.issue["selection_hash"] = digest({key: self.issue[key] for key in (
                    "issue_id", "client_id", "revision", "date", "products", "evidence_fingerprint")})
                with self.assertRaisesRegex(SafetyError, "PRESENTATION_INPUT_CHANGED"):
                    self.run_package(presentation_source_revision=1, selected_language="en")
                self.memory.evidence_type = "synthetic"
        self.assertFalse((self.data.parent / "revision-2/package-input.json").exists())
        self.assertEqual(self.source_bytes(), before)

    def test_presentation_rejects_changed_later_selected_language(self):
        self.issue.update(projected_ordinal=3, language_state="language_pending")
        self.run_package(selected_language="en")
        self.advance_revision()
        with self.assertRaisesRegex(SafetyError, "PRESENTATION_INPUT_CHANGED"):
            self.run_package(presentation_source_revision=1, selected_language="hi-Latn")
        self.assertFalse((self.data.parent / "revision-2/package-input.json").exists())

    def test_presentation_later_hinglish_preserves_internal_english_and_only_one_delivered_edition(self):
        self.issue.update(projected_ordinal=3, language_state="language_pending")
        self.run_package(selected_language="hi-Latn")
        before = self.source_bytes()
        self.advance_revision()
        with patch("product_radar.report_workflow.generate_report_narratives", side_effect=AssertionError("No model")):
            result = self.run_package(presentation_source_revision=1, selected_language="hi-Latn")
        manifest = load_json(result["manifest_path"], 512 * 1024)
        dataset = load_json(self.data.parent / "revision-2/final_report_data.json", 4 * 1024 * 1024)
        self.assertEqual(manifest["languages"], ["hi-Latn"])
        self.assertEqual(set(dataset["narratives"]), {"en", "hi-Latn"})
        self.assertEqual(len(self.renders), 2)
        self.assertEqual(self.source_bytes(), before)

    def test_presentation_rejects_invalid_source_revision_without_calls(self):
        for value in (True, 0, -1, 1, 2, "1"):
            with self.subTest(value=value), self.assertRaisesRegex(SafetyError, "PRESENTATION_REVISION_INVALID"):
                self.run_package(presentation_source_revision=value)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.renders, [])

    def test_presentation_requires_complete_unapproved_source_and_untampered_files(self):
        source = self.run_package()
        before = self.source_bytes()
        manifest = load_json(source["manifest_path"], 512 * 1024)
        self.advance_revision()
        for kind in ("incomplete", "approved", "status", "pdf", "html", "facts", "missing_language", "frozen_facts"):
            with self.subTest(kind=kind):
                for path, raw in before.items():
                    atomic_bytes(path, raw)
                if kind in ("incomplete", "approved"):
                    changed = deepcopy(manifest)
                    changed["complete" if kind == "incomplete" else "approved"] = kind == "approved"
                    atomic_json(self.data / "delivery-manifest.json", changed)
                elif kind == "status":
                    changed = load_json(self.data / "package-status.json", 512 * 1024)
                    changed["input_hash"] = "b" * 64
                    atomic_json(self.data / "package-status.json", changed)
                elif kind in ("pdf", "html"):
                    path = Path(manifest["pdf_receipts"]["en"]["path"] if kind == "pdf" else manifest["language_manifests"]["en"]["artifacts"]["html"]["path"])
                    atomic_bytes(path, path.read_bytes() + b"changed")
                elif kind in ("facts", "missing_language"):
                    changed = load_json(self.data / "final_report_data.json", 4 * 1024 * 1024)
                    if kind == "facts":
                        changed["products"][0]["title"] = "Tampered source fact"
                    else:
                        del changed["narratives"]["hi-Latn"]
                    atomic_json(self.data / "final_report_data.json", changed)
                else:
                    changed = load_json(self.data / "package-input.json", 8 * 1024 * 1024)
                    changed["product_facts"][0]["title"] = "Tampered original input"
                    atomic_json(self.data / "package-input.json", changed)
                with self.assertRaises(SafetyError):
                    self.run_package(presentation_source_revision=1)
                self.assertFalse((self.data.parent / "revision-2/package-input.json").exists())

    def test_presentation_missing_source_does_not_create_destination_or_model_jobs(self):
        self.advance_revision()
        with self.assertRaisesRegex(SafetyError, "SAVED_FILE_UNAVAILABLE"):
            self.run_package(presentation_source_revision=1)
        self.assertEqual(self.runner.calls, [])
        self.assertFalse((self.data.parent / "revision-2/package-input.json").exists())

    def test_presentation_replay_cannot_switch_or_drop_frozen_source(self):
        self.run_package()
        self.advance_revision()
        self.run_package(presentation_source_revision=1)
        before = self.packet_bytes(2)
        lock = self.data.parent / "revision-2/package_writer/runner.lock"
        prior_clock = load_json(lock)["started_at"]
        later_clock = "2099-01-01T00:00:00+00:00"
        self.assertNotEqual(prior_clock, later_clock)
        with patch("product_radar.storage.utc_now", return_value=later_clock):
            with self.assertRaisesRegex(SafetyError, "FROZEN_INPUT_CHANGED"):
                self.run_package()
        self.assertEqual(load_json(lock)["started_at"], later_clock)
        self.assertEqual(self.packet_bytes(2), before)
        self.advance_revision()
        result = self.run_package(presentation_source_revision=2)
        self.assertEqual(result["presentation_source"]["revision"], 2)
        self.assertTrue(result["source_job_ids"])
        self.assertEqual(result["job_ids"], [])


if __name__ == "__main__":
    unittest.main()
