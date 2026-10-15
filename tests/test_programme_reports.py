"""Reports progress beside research; disposable saved facts, no live calls."""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import fitz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_fit_pipeline import NOW, WritingFixture
from test_report_source_facts import source_fixture
from product_radar.issue_allocation import reserve_issue
from product_radar.live_outputs import digest_bytes
from product_radar.models import SafetyError, digest, timestamp
from product_radar.programme import (STAGES, _save, advance_programme, load_programme,
                                     prepare_programme)
from product_radar.research_memory import ResearchMemory, change
from product_radar.service import load_policy
from product_radar.storage import Journal, atomic_bytes, atomic_json, load_json
from test_fit_pipeline import older_format_scores


def inspect(raw):
    with fitz.open(stream=raw, filetype="pdf") as pdf:
        if not len(pdf) or any(page.rect.width >= page.rect.height or not page.get_text().strip() for page in pdf):
            raise SafetyError("SYNTHETIC_PDF_INVALID")
        return {"pages": len(pdf), "portrait": True, "bytes": len(raw)}


@older_format_scores
class ProgrammeReportsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.writer = WritingFixture(); self.renders = []
        self.as_of = NOW

    def seed(self, count=10, *, ready=None, total=None):
        state = source_fixture(count).state
        for assessment in state["assessments"].values():
            assessment["contract_version"] = "research-e-fit10-v2"
        ready = count if ready is None else ready
        self.pending_assessments = {cid: deepcopy(row) for cid, row in state["assessments"].items()
                                    if int(cid[1:]) >= ready}
        for cid in self.pending_assessments:
            del state["assessments"][cid]
        for number in range(count, total or count):
            cid = f"pending{number:03d}"
            state["concepts"][cid] = {"status": "active", "description": {
                "label": "Pending synthetic family " + str(number), "category": "Home and Kitchen",
                "function": "Synthetic unfinished research", "mechanism": "Manual",
                "features": ["Fixture only"], "audience": "Adult", "brand": "generic_or_small",
                "basis": "observed", "reason": "No real product or source request."}}
        self.memory.commit("synthetic-programme-report-inputs", [change(table, ref, row)
            for table, values in state.items() for ref, row in values.items()])
        self.record = prepare_programme(self.memory, "report-programme", source_run_id="report-source-run",
                                        as_of=NOW, concept_ids=sorted(state["concepts"]))
        self.slots = self.record["release_slots"]

    def steps(self, acquire=None):
        steps = {stage: (lambda *_args: {"state": "synthetic_no_new_work"}) for stage in STAGES if stage != "reports"}
        if acquire:
            steps["acquire"] = acquire
        return steps

    def advance(self, *, steps=None, **options):
        return advance_programme(self.memory, "report-programme", root=self.root, handlers={},
            runner=self.writer, render_pdf=self.render, inspect_pdf=inspect, as_of=self.as_of,
            steps=self.steps() if steps is None else steps, **options)

    def render(self, html, pdf):
        self.renders.append(str(pdf))
        with fitz.open() as document:
            page = document.new_page(width=595, height=842)
            page.insert_text((40, 40), "Synthetic programme report. No real products, model or provider.")
            raw = document.tobytes()
        atomic_bytes(pdf, raw)
        return dict(inspect(raw), path=str(pdf), html_sha256=digest_bytes(html.read_bytes()), pdf_sha256=digest_bytes(raw))

    def saved(self):
        return load_programme(self.memory, "report-programme")

    def folder(self, index=0, revision=1):
        return self.root / "data/outputs" / self.slots[index]["issue_id"] / ("revision-" + str(revision))

    def packet_bytes(self, index=0, revision=1):
        folder = self.folder(index, revision)
        return {path.relative_to(self.root): path.read_bytes() for path in folder.rglob("*")
                if path.is_file() and path.name in {"package-input.json", "final_report_data.json",
                    "delivery-manifest.json", "shortlist.xlsx", "report-en.pdf", "report-hi-Latn.pdf"}}

    def unrelated_progress(self, counter):
        counter.append(1)
        self.memory.put("observations", "synthetic-unmatched-" + str(len(counter)), {
            "source": "meta", "source_item": "unmatched-" + str(len(counter)),
            "acquisition_id": "synthetic-unmatched", "observed_at": self.as_of,
            "semantic_id": "unmatched-" + str(len(counter)), "semantic": {"title": "Unmatched fixture"},
            "facts": {"url": "https://example.org/unmatched", "limitations": []}})

    def accept_pending(self):
        self.memory.commit("synthetic-newly-ready-assessments", [change("assessments", cid, row)
            for cid, row in self.pending_assessments.items()])

    def refresh_selected(self):
        original = self.memory.snapshot()["observations"]["meta000"]
        newer = deepcopy(original)
        newer.update(observed_at=self.as_of, acquisition_id="synthetic-new-selected-capture",
                     capture_id="synthetic-new-selected-capture")
        newer["facts"]["active"] = False
        self.memory.commit("synthetic-selected-source-refresh", [
            change("captures", newer["capture_id"], {"source": "meta", "acquisition_id": newer["acquisition_id"],
                "observed_at": self.as_of, "reference": {"route": "meta-ads"}}),
            change("observations", "synthetic-new-selected-meta", newer)])

    def test_first_ten_product_draft_runs_while_full_156_research_advances(self):
        self.seed(total=156); calls = []
        def acquire(*_args):
            self.unrelated_progress(calls)
            return {"state": "synthetic_work_remaining"}
        result = self.advance(steps=self.steps(acquire), max_cycles=2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["progress"]["products"], 156)
        self.assertEqual(result["progress"]["fit_states"], {"accepted": 10, "needs_review": 146})
        first = self.saved()["drafts"][self.slots[0]["issue_id"]]
        self.assertIs(first["complete"], True)
        self.assertEqual(first["products"], 10)
        self.assertEqual(first["revision"], 1)
        self.assertEqual(len(self.renders), 2)
        self.assertGreater(self.writer.calls, 0)
        self.assertEqual(self.memory.snapshot()["published_history"], {})
        hold = self.memory.snapshot()["issue_reservations"][self.slots[0]["issue_id"]]
        self.assertEqual(hold["state"], "reserved")
        self.assertEqual(hold["counts"], {"products": 10, "new": 10, "updates": 0})
        manifest = load_json(self.folder() / "delivery-manifest.json", 512 * 1024)
        self.assertEqual(manifest["languages"], ["en", "hi-Latn"])
        self.assertIs(manifest["approved"], False)
        self.assertIs(manifest["release_ready"], False)
        # The reports stage saves the owner's score tally for the desktop line.
        from product_radar.opportunity_score import score_line
        self.assertTrue(score_line(self.memory.root).startswith(
            "Opportunity score: 10 types scored (10 sweet spot, 0 early bet, 0 proven but crowded, 0 too quiet)"))

    def test_score_tally_failure_never_stops_the_reports_stage(self):
        self.seed()
        with patch("product_radar.opportunity_score.record_score_summary", side_effect=OSError("disk full")):
            self.advance(max_cycles=1)
        self.assertIs(self.saved()["drafts"][self.slots[0]["issue_id"]]["complete"], True)
        with patch("product_radar.opportunity_score.record_score_summary", side_effect=SafetyError("RESEARCH_EVENTS_INVALID")), \
                self.assertRaises(SafetyError):
            self.advance(max_cycles=1)

    def test_later_missing_issue_fills_without_duplicating_or_rewriting_first(self):
        self.seed(20, ready=10)
        self.advance(max_cycles=1)
        original, calls = self.packet_bytes(), self.writer.calls
        self.accept_pending()
        progress = []
        result = self.advance(steps=self.steps(lambda *_args: self.unrelated_progress(progress) or {}), max_cycles=2)
        reservations = self.memory.snapshot()["issue_reservations"]
        first, second = (reservations[self.slots[index]["issue_id"]] for index in (0, 1))
        self.assertEqual({row["concept_id"] for row in first["products"]} &
                         {row["concept_id"] for row in second["products"]}, set())
        self.assertEqual([first["projected_ordinal"], second["projected_ordinal"]], [1, 2])
        self.assertEqual([first["revision"], second["revision"]], [1, 1])
        self.assertTrue(all(self.root.joinpath(path).read_bytes() == raw for path, raw in original.items()))
        self.assertEqual(len(self.renders), 4)
        self.assertGreater(self.writer.calls, calls)
        self.assertEqual(len(progress), 2)
        self.assertFalse(result["client_publication_attempted"])

    def test_complete_packet_and_lost_programme_draft_state_replay_without_g_or_pdf(self):
        self.seed(); self.advance(max_cycles=1)
        original, calls, renders = self.packet_bytes(), self.writer.calls, len(self.renders)
        record = self.saved(); del record["drafts"][self.slots[0]["issue_id"]]; _save(self.memory, record)
        self.memory = ResearchMemory(self.memory.root, self.memory.journal, evidence_type="synthetic")
        progress = []
        self.advance(steps=self.steps(lambda *_args: self.unrelated_progress(progress) or {}), max_cycles=2)
        first = self.saved()["drafts"][self.slots[0]["issue_id"]]
        self.assertTrue(first["complete"])
        self.assertTrue(first["reused"])
        self.assertEqual(first["revision"], 1)
        self.assertEqual((self.writer.calls, len(self.renders)), (calls, renders))
        self.assertTrue(all(self.root.joinpath(path).read_bytes() == raw for path, raw in original.items()))

    def test_partial_pdf_resumes_original_facts_and_keeps_healthy_edition(self):
        self.seed(); original_render = self.render
        def fail_hinglish(html, pdf):
            if pdf.name == "report-hi-Latn.pdf":
                raise OSError("Synthetic temporary render failure")
            return original_render(html, pdf)
        with patch.object(self, "render", side_effect=fail_hinglish):
            self.advance(max_cycles=1)
        status = self.saved()["drafts"][self.slots[0]["issue_id"]]
        self.assertFalse(status["complete"])
        self.assertEqual(status["stage_outcomes"]["editions"]["en"]["state"], "completed")
        english = Path(status["stage_outcomes"]["editions"]["en"]["pdf"]["path"])
        en_bytes, input_bytes, calls = english.read_bytes(), (self.folder() / "package-input.json").read_bytes(), self.writer.calls
        self.as_of = (timestamp(NOW) + timedelta(seconds=301)).isoformat()
        progress = []
        self.advance(steps=self.steps(lambda *_args: self.unrelated_progress(progress) or {}), max_cycles=1)
        resumed = self.saved()["drafts"][self.slots[0]["issue_id"]]
        self.assertTrue(resumed["complete"])
        self.assertEqual(resumed["revision"], 1)
        self.assertEqual(self.writer.calls, calls)
        self.assertEqual(len(self.renders), 2)
        self.assertEqual(english.read_bytes(), en_bytes)
        self.assertEqual((self.folder() / "package-input.json").read_bytes(), input_bytes)

    def test_changed_selected_source_revises_only_that_issue_while_research_continues(self):
        self.seed(20, ready=10); self.advance(max_cycles=1)
        original, calls = self.packet_bytes(), self.writer.calls
        changed = []
        def acquire(*_args):
            if not changed:
                self.refresh_selected(); self.accept_pending(); changed.append(1)
            return {"state": "synthetic_work_remaining"}
        self.advance(steps=self.steps(acquire), max_cycles=1)
        first = self.saved()["drafts"][self.slots[0]["issue_id"]]
        second = self.saved()["drafts"][self.slots[1]["issue_id"]]
        self.assertTrue(first["complete"])
        self.assertEqual(first["revision"], 2)
        self.assertEqual(first["retained_revision"], 1)
        self.assertTrue(first["retained_complete"])
        self.assertTrue(second["complete"])
        self.assertEqual(len(self.renders), 6)
        self.assertTrue(all(self.root.joinpath(path).read_bytes() == raw for path, raw in original.items()))
        revised_calls, revised_renders = self.writer.calls, len(self.renders)
        self.advance(max_cycles=1)
        first = self.saved()["drafts"][self.slots[0]["issue_id"]]
        self.assertTrue(first["complete"])
        self.assertEqual(first["revision"], 2)
        self.assertEqual(self.memory.snapshot()["issue_reservations"][self.slots[0]["issue_id"]]["revision"], 2)
        self.assertGreater(self.writer.calls, calls)
        self.assertEqual((self.writer.calls, len(self.renders)), (revised_calls, revised_renders))
        self.assertTrue(all(self.root.joinpath(path).read_bytes() == raw for path, raw in original.items()))
        self.assertEqual(self.saved()["drafts"][self.slots[1]["issue_id"]]["revision"], 1)

    def test_reservation_survives_lost_reply_before_package_and_restarts_same_revision(self):
        self.seed(); calls = []
        def lost_reply(*args, **kwargs):
            result = reserve_issue(*args, **kwargs)
            calls.append(1)
            if len(calls) == 1:
                raise OSError("Synthetic reply lost after genuine reservation")
            return result
        with patch("product_radar.issue_allocation.reserve_issue", side_effect=lost_reply):
            self.advance(max_cycles=1)
        self.assertEqual(self.writer.calls, 0)
        self.assertFalse((self.folder() / "package-input.json").exists())
        self.assertEqual(self.memory.snapshot()["issue_reservations"][self.slots[0]["issue_id"]]["revision"], 1)
        self.as_of = (timestamp(NOW) + timedelta(seconds=301)).isoformat()
        self.memory = ResearchMemory(self.memory.root, self.memory.journal, evidence_type="synthetic")
        self.advance(max_cycles=1)
        first = self.saved()["drafts"][self.slots[0]["issue_id"]]
        self.assertTrue(first["complete"])
        self.assertEqual(first["revision"], 1)
        self.assertEqual(len(self.renders), 2)
        self.assertNotIn("state", load_json(self.folder() / "package-input.json", 8 * 1024 * 1024)["issue"])
        self.assertFalse(self.folder(revision=2).exists())

    def test_real_source_change_after_reservation_without_input_advances_revision_not_stuck(self):
        self.seed()
        with patch("product_radar.report_workflow.prepare_review_package", side_effect=OSError("Synthetic input save failed")):
            self.advance(max_cycles=1)
        self.refresh_selected()
        self.as_of = (timestamp(NOW) + timedelta(seconds=301)).isoformat()
        self.advance(max_cycles=1)
        first = self.saved()["drafts"][self.slots[0]["issue_id"]]
        self.assertTrue(first["complete"])
        self.assertEqual(first["revision"], 2)
        self.assertFalse((self.folder() / "package-input.json").exists())
        self.assertTrue((self.folder(revision=2) / "package-input.json").exists())

    def test_uploaded_published_or_cancelled_issue_cannot_be_automatically_rebuilt_or_restaged(self):
        for state in ("uploaded", "published", "cancelled"):
            with self.subTest(state=state):
                self.seed(); self.advance(max_cycles=1)
                calls, renders, original = self.writer.calls, len(self.renders), self.packet_bytes()
                ref = self.slots[0]["issue_id"]
                old = self.memory.snapshot()["issue_reservations"][ref]
                self.memory.put("issue_reservations", ref, dict(old, state=state), previous=old)
                self.advance(max_cycles=3)
                draft = self.saved()["drafts"][ref]
                self.assertEqual(draft["state"], "protected_issue")
                self.assertFalse(draft["complete"])
                self.assertTrue(draft["retained_complete"])
                if state != "cancelled":
                    self.assertEqual((self.writer.calls, len(self.renders)), (calls, renders))
                else:
                    # Cancellation deliberately releases the existing hold.
                    # A different missing logical issue may use those items;
                    # the cancelled original must not be recreated.
                    self.assertTrue(all(self.slots[0]["issue_id"] not in path
                                        for path in self.renders[renders:]))
                    self.assertFalse(self.folder(revision=2).exists())
                    self.assertEqual((self.writer.calls, len(self.renders)), (calls + 6, renders + 2))
                    self.assertFalse(self.folder(1, revision=2).exists())
                self.assertTrue(all(self.root.joinpath(path).read_bytes() == raw for path, raw in original.items()))
                # Next subcase uses a fresh isolated store, not live cleanup.
                if state != "cancelled":
                    self.temp.cleanup(); self.setUp()

    def test_external_hold_conflict_isolated_while_another_missing_issue_completes(self):
        self.seed(20, ready=10); self.advance(max_cycles=1)
        ref = self.slots[0]["issue_id"]
        first = self.memory.snapshot()["issue_reservations"][ref]
        selected = deepcopy(first["products"][0])
        self.memory.put("issue_reservations", "synthetic-external-hold", {
            "issue_id": "synthetic-external-hold", "date": "2026-10-20", "state": "reserved",
            "client_id": "default", "products": [selected]})
        self.accept_pending(); self.advance(max_cycles=1)
        drafts = self.saved()["drafts"]
        self.assertFalse(drafts[ref]["complete"])
        self.assertEqual(drafts[ref]["reason"], "ISSUE_DUPLICATE_OR_QUEUED_PRODUCT")
        self.assertTrue(drafts[self.slots[1]["issue_id"]]["complete"])
        self.assertEqual(self.memory.snapshot()["published_history"], {})

    def test_new_publication_history_conflict_isolated_without_relabelling_frozen_new_items(self):
        self.seed(20, ready=10); self.advance(max_cycles=1)
        first_id = self.slots[0]["issue_id"]
        original = self.packet_bytes()
        self.memory.put("published_history", "synthetic-separate-publication", {
            "status": "published", "client_id": "default", "concept_id": "c000",
            "issue_id": "synthetic-other-published-issue", "ordinal": 1})
        self.accept_pending()
        # The unrelated published issue genuinely consumes a language ordinal.
        # Supply the ongoing language in this fixture instead of hiding that
        # change or automatically declaring a third logical issue bilingual.
        policy = load_policy(); policy["ongoing_pdf_language"] = "en"
        with patch("product_radar.service.load_policy", return_value=policy):
            self.advance(max_cycles=1)
        drafts = self.saved()["drafts"]
        self.assertFalse(drafts[first_id]["complete"])
        self.assertEqual(drafts[first_id]["reason"], "ISSUE_NOVELTY_MISMATCH")
        self.assertTrue(drafts[self.slots[1]["issue_id"]]["complete"])
        self.assertEqual(self.memory.snapshot()["issue_reservations"][first_id]["revision"], 1)
        self.assertTrue(all(self.root.joinpath(path).read_bytes() == raw for path, raw in original.items()))
        self.assertFalse(self.folder(revision=2).exists())

    def test_third_issue_keeps_its_real_workbook_and_language_pending(self):
        self.seed(30); self.advance(max_cycles=1)
        record = self.saved()
        first, second, third = [record["drafts"][slot["issue_id"]] for slot in self.slots]
        self.assertTrue(first["complete"] and second["complete"])
        self.assertEqual(third["state"], "language_pending")
        self.assertFalse(third["complete"])
        self.assertEqual(third["stage_outcomes"]["workbook"]["state"], "completed")
        workbook = Path(third["stage_outcomes"]["workbook"]["receipt"]["artifacts"]["workbook"]["path"])
        self.assertTrue(workbook.is_file())
        self.assertEqual(len(self.renders), 4)
        self.assertFalse((self.folder(2) / "delivery-manifest.json").exists())
        frozen = load_json(self.folder(2) / "package-input.json", 8 * 1024 * 1024)
        self.assertEqual(frozen["issue"]["projected_ordinal"], 3)
        self.assertEqual(frozen["issue"]["revision"], 1)
        calls, original = self.writer.calls, workbook.read_bytes()
        self.as_of = (timestamp(NOW) + timedelta(seconds=301)).isoformat()
        self.advance(max_cycles=1)
        self.assertEqual(self.writer.calls, calls)
        self.assertEqual(workbook.read_bytes(), original)

    def test_shortfall_cannot_reserve_or_launch_g_even_while_research_changes(self):
        self.seed(9); progress = []
        self.advance(steps=self.steps(lambda *_args: self.unrelated_progress(progress) or {}), max_cycles=2)
        self.assertEqual(self.writer.calls, 0)
        self.assertEqual(self.renders, [])
        self.assertEqual(self.memory.snapshot()["issue_reservations"], {})
        self.assertTrue(all(row["state"] == "shortfall" for row in self.saved()["drafts"].values()))
        self.assertFalse((self.root / "data/outputs").exists())

    def test_bad_saved_input_keeps_original_hold_and_does_not_stop_other_missing_issue(self):
        self.seed(20, ready=10); self.advance(max_cycles=1)
        original = self.packet_bytes()
        atomic_bytes(self.folder() / "package-input.json", b'{"bad synthetic input":')
        self.accept_pending(); self.advance(max_cycles=1)
        first, second = [self.saved()["drafts"][slot["issue_id"]] for slot in self.slots[:2]]
        self.assertFalse(first["complete"])
        self.assertEqual(first["reason"], "JSON_RESPONSE_INVALID")
        self.assertTrue(second["complete"])
        old_ids = {row["concept_id"] for row in self.memory.snapshot()["issue_reservations"][self.slots[0]["issue_id"]]["products"]}
        new_ids = {row["concept_id"] for row in self.memory.snapshot()["issue_reservations"][self.slots[1]["issue_id"]]["products"]}
        self.assertFalse(old_ids & new_ids)
        self.assertTrue(all(self.root.joinpath(path).read_bytes() == raw for path, raw in original.items()
                            if path.name != "package-input.json"))

    def test_new_established_brand_exclusion_is_not_hidden_by_a_frozen_complete_packet(self):
        self.seed(20, ready=10); self.advance(max_cycles=1)
        original = self.packet_bytes(); changed = []
        def acquire(*_args):
            if not changed:
                old = self.memory.snapshot()["concepts"]["c000"]
                new = deepcopy(old); new["description"]["brand"] = "established"
                self.memory.put("concepts", "c000", new, previous=old)
                self.accept_pending(); changed.append(1)
            return {"state": "synthetic_work_remaining"}
        self.advance(steps=self.steps(acquire), max_cycles=1)
        first, second = [self.saved()["drafts"][slot["issue_id"]] for slot in self.slots[:2]]
        self.assertTrue(first["complete"])
        self.assertEqual(first["revision"], 2)
        self.assertTrue(first["retained_complete"])
        # Nineteen usable products can fill the earliest ten-product issue,
        # not two issues. Do not strand that repaired earlier issue merely
        # because the removed established brand belonged to its first copy.
        self.assertEqual(second["state"], "shortfall")
        self.assertEqual(second["shortfall"], 1)
        hold = self.memory.snapshot()["issue_reservations"][self.slots[0]["issue_id"]]
        self.assertEqual(len(hold["products"]), 10)
        self.assertNotIn("c000", {row["concept_id"] for row in hold["products"]})
        self.assertNotIn(self.slots[1]["issue_id"], self.memory.snapshot()["issue_reservations"])
        self.assertTrue(all(self.root.joinpath(path).read_bytes() == raw for path, raw in original.items()))

    def test_expired_selected_evidence_does_not_get_a_fresh_date_or_false_readiness(self):
        self.seed(); self.advance(max_cycles=1)
        original, calls = self.packet_bytes(), self.writer.calls
        self.as_of = "2026-10-14T10:00:00+00:00"
        self.advance(max_cycles=1)
        first = self.saved()["drafts"][self.slots[0]["issue_id"]]
        self.assertFalse(first.get("complete", False))
        self.assertEqual(first["state"], "shortfall")
        self.assertTrue(first["retained_complete"])
        self.assertEqual(first["retained_revision"], 1)
        self.assertEqual(self.writer.calls, calls)
        self.assertTrue(all(self.root.joinpath(path).read_bytes() == raw for path, raw in original.items()))
        self.assertFalse((self.folder(revision=2) / "package-input.json").exists())

    def test_later_language_choice_resumes_same_third_input_without_touching_first_two(self):
        self.seed(30); self.advance(max_cycles=1)
        original = {**self.packet_bytes(0), **self.packet_bytes(1)}
        frozen = (self.folder(2) / "package-input.json").read_bytes()
        pending = self.saved()["drafts"][self.slots[2]["issue_id"]]
        workbook = Path(pending["stage_outcomes"]["workbook"]["receipt"]["artifacts"]["workbook"]["path"])
        workbook_bytes = workbook.read_bytes(); calls, renders = self.writer.calls, len(self.renders)
        policy = load_policy(); policy["ongoing_pdf_language"] = "en"
        with patch("product_radar.service.load_policy", return_value=policy):
            self.advance(max_cycles=1)
        third = self.saved()["drafts"][self.slots[2]["issue_id"]]
        self.assertTrue(third["complete"])
        self.assertEqual(third["revision"], 1)
        manifest = load_json(self.folder(2) / "delivery-manifest.json", 512 * 1024)
        self.assertEqual(manifest["languages"], ["en"])
        self.assertEqual(manifest["selected_language"], "en")
        self.assertEqual(len(self.renders), renders + 1)
        self.assertGreater(self.writer.calls, calls)
        self.assertEqual((self.folder(2) / "package-input.json").read_bytes(), frozen)
        self.assertEqual(workbook.read_bytes(), workbook_bytes)
        self.assertTrue(all(self.root.joinpath(path).read_bytes() == raw for path, raw in original.items()))

    def test_a_different_saved_reservation_is_not_overwritten_to_resume_old_package(self):
        self.seed(); self.advance(max_cycles=1)
        original, calls, renders = self.packet_bytes(), self.writer.calls, len(self.renders)
        ref = self.slots[0]["issue_id"]
        old = self.memory.snapshot()["issue_reservations"][ref]
        changed = deepcopy(old); changed.update(revision=2, selection_hash="f" * 64)
        self.memory.put("issue_reservations", ref, changed, previous=old)
        self.advance(max_cycles=1)
        draft = self.saved()["drafts"][ref]
        self.assertFalse(draft["complete"])
        self.assertEqual(draft["reason"], "REPORT_RESERVATION_CHANGED")
        self.assertEqual(self.memory.snapshot()["issue_reservations"][ref], changed)
        self.assertEqual((self.writer.calls, len(self.renders)), (calls, renders))
        self.assertTrue(all(self.root.joinpath(path).read_bytes() == raw for path, raw in original.items()))

    def test_foreign_frozen_input_is_not_used_as_authority_for_a_new_g_job(self):
        self.seed(); self.advance(max_cycles=1)
        calls, renders = self.writer.calls, len(self.renders)
        path = self.folder() / "package-input.json"
        saved = load_json(path, 8 * 1024 * 1024)
        saved["issue"]["client_id"] = "other-client"
        atomic_json(path, saved)
        self.advance(max_cycles=1)
        draft = self.saved()["drafts"][self.slots[0]["issue_id"]]
        self.assertFalse(draft["complete"])
        self.assertEqual(draft["reason"], "REPORT_FROZEN_INPUT_INVALID")
        self.assertEqual((self.writer.calls, len(self.renders)), (calls, renders))
        self.assertFalse(self.folder(revision=2).exists())

    def test_source_race_before_reservation_does_not_pin_a_nonexistent_writing_input(self):
        self.seed(); calls = []
        def changed_before_hold(*args, **kwargs):
            if not calls:
                calls.append(1); self.refresh_selected()
            return reserve_issue(*args, **kwargs)
        with patch("product_radar.issue_allocation.reserve_issue", side_effect=changed_before_hold):
            self.advance(max_cycles=1)
        draft = self.saved()["drafts"][self.slots[0]["issue_id"]]
        self.assertEqual(draft["reason"], "ISSUE_SOURCE_FACTS_CHANGED")
        self.assertIs(draft["selection_reserved"], False)
        self.assertEqual(self.memory.snapshot()["issue_reservations"], {})
        self.assertEqual(self.writer.calls, 0)
        self.assertFalse((self.folder() / "package-input.json").exists())
        self.as_of = (timestamp(NOW) + timedelta(seconds=301)).isoformat()
        self.advance(max_cycles=1)
        draft = self.saved()["drafts"][self.slots[0]["issue_id"]]
        self.assertTrue(draft["complete"])
        self.assertIs(draft["selection_reserved"], True)
        self.assertEqual(draft["revision"], 1)
        self.assertEqual(len(self.renders), 2)

    def test_busy_before_reservation_retries_fresh_selection_without_g_on_failed_attempt(self):
        self.seed()
        with patch("product_radar.issue_allocation.reserve_issue", side_effect=SafetyError("RUNNER_ALREADY_ACTIVE")):
            self.advance(max_cycles=1)
        draft = self.saved()["drafts"][self.slots[0]["issue_id"]]
        self.assertIs(draft["selection_reserved"], False)
        self.assertEqual(self.writer.calls, 0)
        self.assertEqual(self.memory.snapshot()["issue_reservations"], {})
        self.as_of = (timestamp(NOW) + timedelta(seconds=301)).isoformat()
        self.advance(max_cycles=1)
        draft = self.saved()["drafts"][self.slots[0]["issue_id"]]
        self.assertTrue(draft["complete"])
        self.assertIs(draft["selection_reserved"], True)
        self.assertEqual(draft["revision"], 1)

    def test_valid_legacy_packet_is_really_reserved_before_later_missing_issue_ordinal(self):
        self.seed(20, ready=10)
        # A pre-reservation programme wrote this legitimate frozen packet
        # without a canonical hold. Simulate that older call boundary only;
        # immutable memory records are never deleted to manufacture a legacy
        # state. Recovery below uses the actual reservation implementation.
        with patch("product_radar.issue_allocation.reserve_issue", side_effect=lambda _memory, issue, **_options: issue):
            self.advance(max_cycles=1)
        original, calls = self.packet_bytes(), self.writer.calls
        ref = self.slots[0]["issue_id"]
        self.assertEqual(self.memory.snapshot()["issue_reservations"], {})
        record = self.saved(); record["drafts"][ref].pop("selection_reserved"); _save(self.memory, record)
        self.accept_pending()
        self.advance(max_cycles=3)
        reservations = self.memory.snapshot()["issue_reservations"]
        first, second = [reservations[slot["issue_id"]] for slot in self.slots[:2]]
        self.assertEqual([first["projected_ordinal"], second["projected_ordinal"]], [1, 2])
        self.assertEqual([first["revision"], second["revision"]], [1, 1])
        self.assertEqual((self.writer.calls, len(self.renders)), (calls + 6, 4))
        self.assertTrue(all(self.root.joinpath(path).read_bytes() == raw for path, raw in original.items()))
        self.assertFalse(self.folder(1, revision=2).exists())

    def test_busy_legacy_hold_adoption_defers_only_dependent_report_order_then_recovers(self):
        self.seed(20, ready=10)
        with patch("product_radar.issue_allocation.reserve_issue", side_effect=lambda _memory, issue, **_options: issue):
            self.advance(max_cycles=1)
        original, calls = self.packet_bytes(), self.writer.calls
        ref = self.slots[0]["issue_id"]
        self.assertEqual(self.memory.snapshot()["issue_reservations"], {})
        record = self.saved(); record["drafts"][ref].pop("selection_reserved"); _save(self.memory, record)
        self.accept_pending(); progress = []
        with patch("product_radar.issue_allocation.reserve_issue", side_effect=SafetyError("RUNNER_ALREADY_ACTIVE")):
            self.advance(steps=self.steps(lambda *_args: self.unrelated_progress(progress) or {}), max_cycles=1)
        self.assertEqual(len(progress), 1)
        self.assertEqual(self.writer.calls, calls)
        self.assertEqual(self.memory.snapshot()["issue_reservations"], {})
        self.advance(max_cycles=1)
        second = self.memory.snapshot()["issue_reservations"][self.slots[1]["issue_id"]]
        self.assertEqual(second["projected_ordinal"], 2)
        self.assertTrue(all(self.root.joinpath(path).read_bytes() == raw for path, raw in original.items()))


if __name__ == "__main__":
    unittest.main()
