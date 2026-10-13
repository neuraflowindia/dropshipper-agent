"""Private legacy-to-type report transition, using only disposable real writers."""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import product_radar.programme as programme
from product_radar.draft_automation import private_staging_callback
from product_radar.fit_review import run_fit_reviews
from product_radar.functional_aliases import accept_functional_alias, prepare_alias_input
from product_radar.functional_types import (DEFINITION_FIELDS, accept_functional_definition,
    effective_functional_definition)
from product_radar.issue_allocation import allocate_issues, allocation_snapshot_hash, reserve_issue
from product_radar.models import SafetyError, digest, timestamp
from product_radar.report_examples import RECOMMENDATION_CONTRACT
from product_radar.report_selection import build_report_pool
from product_radar.report_workflow import freeze_review_input
from product_radar.research_flow import run_flow
from product_radar.research_memory import ResearchMemory, change, import_capture
from product_radar.research_sources import map_research_rows
from product_radar.storage import Journal, load_json
from product_radar.work_turns import bind_work_turn, close_work_turn, open_work_turn
import test_functional_source_market as functional
from test_research_sources import amazon_row, request
from test_functional_fit import FunctionalFitRunner
import test_private_drafts as private_fixtures
import test_programme_reports as legacy


class ProgrammeLegacyUpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(); cls.addClassCleanup(temporary.cleanup)
        # The shared helper defaults to a ten-item description pass. Use its
        # ordinary supported finite limit to create twenty genuine fixture
        # types, rather than copying or inventing eligible allocator records.
        def describe_all(memory, runner, **options):
            return run_flow(memory, runner, limit=50, **options)
        def review_all(memory, runner, review_ids):
            return run_fit_reviews(memory, runner, review_ids, limit=20)
        class PrioritizedFitRunner(FunctionalFitRunner):
            def __init__(self):
                super().__init__(); self.priorities = {}

            def __call__(self, role, payload, folder):
                response, usage = super().__call__(role, payload, folder)
                for row in response["results"]:
                    if row["id"] not in self.priorities:
                        self.priorities[row["id"]] = 1 if len(self.priorities) < 10 else 2
                    if self.priorities[row["id"]] == 2:
                        for criterion in row["criteria"]:
                            if criterion["id"] not in {1, 2, 5, 6, 13}:
                                criterion.update(state="Does not meet", reason=
                                    "Estimated: This fictional use does not support this optional practical point.")
                return response, usage
        with patch.object(functional, "run_flow", side_effect=describe_all), \
                patch.object(functional, "run_fit_reviews", side_effect=review_all), \
                patch.object(functional, "FunctionalFitRunner", PrioritizedFitRunner):
            memory, cls.functional_ids = functional.functional_pool_fixture(Path(temporary.name), count=20)
        cls.functional_state = memory.snapshot()
        pool = build_report_pool(memory, as_of=functional.NOW, recommendation_contract=RECOMMENDATION_CONTRACT)
        if len(pool["eligible"]) != 20:
            raise AssertionError((len(pool["eligible"]), pool["decisions"]))
        if sorted(row["opportunity_priority"] for row in pool["eligible"]) != [1] * 10 + [2] * 10:
            raise AssertionError("The genuine fixture must contain ten priority-1 and ten priority-2 types.")

    def setUp(self):
        self.case = legacy.ProgrammeReportsTests(); self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.memory = ResearchMemory(self.case.root / "research", Journal(self.case.root / "finance"),
                                         evidence_type="captured")
        # All pages/model replies are still fictional. Captured is used only
        # to run the real draft-only private wire validator in this namespace.
        self.case.seed(count=20)
        self.store = private_fixtures.Store()
        self.private = private_staging_callback(self.case.root, self.store)
        steps = self.case.steps(); steps.pop("private_staging")
        self.case.advance(max_cycles=1, steps=steps, private_staging=self.private)
        self.record = self.case.saved()
        self.slots = self.record["release_slots"]
        self.original_bytes = {index: self.case.packet_bytes(index) for index in (0, 1)}
        self.original_holds = deepcopy(self.case.memory.snapshot()["issue_reservations"])

    def join(self, count=10, *, source_state=None, source_ids=None):
        source_state = self.functional_state if source_state is None else source_state
        source_ids = self.functional_ids if source_ids is None else source_ids
        current = self.case.memory.snapshot()
        self.assertFalse([(table, ref) for table, rows in source_state.items()
                          for ref in rows if ref in current[table]])
        self.case.memory.commit("join-authentic-functional-fixtures", [change(table, ref, row)
            for table, rows in source_state.items() for ref, row in rows.items()])
        ids = source_ids[:count]
        admitted = programme._register_discovered_products(self.case.memory, self.record,
            {"new_concept_ids": ids, "new_products_need_full_source_review": ids}, self.case.as_of)
        self.assertEqual(set(admitted), set(ids))
        return ids

    def context(self):
        return {"at": self.case.as_of, "root": self.case.root, "runner": self.case.writer,
                "render_pdf": self.case.render, "inspect_pdf": legacy.inspect, "retry_seconds": 300}

    def run_reports(self, *, private=False):
        memory = self.case.memory
        turn = open_work_turn(memory, self.record["programme_id"], at=self.case.as_of, report_ready=True)
        try:
            with bind_work_turn(memory, turn["id"]):
                result = programme._reports(memory, self.record, self.context())
                if private:
                    programme._private_staging(memory, self.record,
                        dict(self.context(), private_staging=self.private))
                programme._save(memory, self.record)
                return result
        finally:
            close_work_turn(memory, turn["id"], at=self.case.as_of, reason="Disposable upgrade regression pass")

    def input(self, index):
        draft = self.record["drafts"][self.slots[index]["issue_id"]]
        return load_json(self.case.folder(index, draft["revision"]) / "package-input.json", 8 * 1024 * 1024)

    def assert_originals_unchanged(self):
        for index, before in self.original_bytes.items():
            self.assertEqual(self.case.packet_bytes(index, revision=1), before)
        self.assertEqual(self.case.memory.snapshot()["published_history"], {})
        self.assertFalse([row for row in self.store.calls if row[0] == "index_put"])

    def unheld_upgrade(self):
        self.join(10)
        ref = self.slots[0]["issue_id"]
        def reject(memory, issue, **options):
            if issue["issue_id"] == ref and issue["revision"] == 2:
                raise SafetyError("FIXTURE_NEW_HOLD_REJECTED")
            return reserve_issue(memory, issue, **options)
        with patch("product_radar.issue_allocation.reserve_issue", side_effect=reject):
            self.run_reports()
        original = load_json(self.case.folder(0, 2) / "package-input.json", 8 * 1024 * 1024)
        self.case.as_of = (timestamp(self.case.as_of) + timedelta(seconds=301)).isoformat()
        state = self.case.memory.snapshot()
        pool = build_report_pool(self.case.memory, as_of=self.case.as_of, recommendation_contract=RECOMMENDATION_CONTRACT)
        eligible = programme._reporting_eligible(self.record, state, pool)
        issue = programme._functional_report_allocations(eligible, self.slots, state)[0]
        facts = [pool["facts_by_concept"][row["concept_id"]] for row in issue["products"]]
        options = dict(state=state, record=self.record, eligible={row["concept_id"]: row for row in eligible},
            reservation=state["issue_reservations"][ref], evidence_type="captured", language=None,
            at=self.case.as_of)
        return original, issue, facts, options

    def test_ten_ready_types_upgrade_only_first_private_legacy_issue_and_reuse(self):
        self.assertEqual(sum(row["state"] == "private_bytes_verified"
            for row in self.record["private_staging_receipts"].values()), 2)
        self.assertTrue(all(row["state"] == "reserved" for row in self.original_holds.values()))
        ids = self.join(10)
        self.run_reports(private=True)
        first, second = self.input(0), self.input(1)
        self.assertEqual(first["issue"]["recommendation_contract"], RECOMMENDATION_CONTRACT)
        self.assertEqual(first["issue"]["revision"], 2)
        self.assertEqual({row["concept_id"] for row in first["issue"]["products"]}, set(ids))
        self.assertEqual(second["issue"]["revision"], 1)
        self.assertNotIn("recommendation_contract", second["issue"])
        self.assertTrue(self.record["drafts"][self.slots[0]["issue_id"]]["complete"])
        calls, renders = self.case.writer.calls, len(self.case.renders)
        self.case.memory = ResearchMemory(self.case.memory.root, self.case.memory.journal, evidence_type="captured")
        self.record = self.case.saved()
        self.run_reports(private=True)
        self.assertEqual((self.case.writer.calls, len(self.case.renders)), (calls, renders))
        self.assertEqual(self.input(0), first)
        self.assertEqual(self.input(1), second)
        self.assert_originals_unchanged()

    def test_twenty_types_upgrade_two_disjoint_bilingual_slots_not_later_slot(self):
        ids = self.join(20)
        self.run_reports()
        # The standard finite ten-execution turn may split report prose. A
        # second ordinary due turn must finish the same frozen revisions;
        # the ordinary retry delay is not bypassed by a direct same-clock call.
        self.case.as_of = (timestamp(self.case.as_of) + timedelta(seconds=301)).isoformat()
        self.run_reports(private=True)
        packets = [self.input(index) for index in (0, 1)]
        self.assertEqual([row["issue"]["revision"] for row in packets], [2, 2])
        self.assertEqual([row["issue"]["projected_ordinal"] for row in packets], [1, 2])
        self.assertTrue(all(row["issue"]["recommendation_contract"] == RECOMMENDATION_CONTRACT for row in packets))
        selected = [row["concept_id"] for packet in packets for row in packet["issue"]["products"]]
        self.assertEqual(len(selected), len(set(selected)))
        self.assertEqual(set(selected), set(ids))
        self.assertEqual([[row["opportunity_priority"] for row in packet["issue"]["products"]]
                          for packet in packets], [[1] * 10, [2] * 10])
        for index in (0, 1):
            draft = self.record["drafts"][self.slots[index]["issue_id"]]
            self.assertTrue(draft["complete"], draft)
            manifest = load_json(Path(draft["manifest_path"]), 512 * 1024)
            self.assertEqual(manifest["languages"], ["en", "hi-Latn"])
            self.assertEqual(manifest["recommendation_contract"], RECOMMENDATION_CONTRACT)
        self.assert_originals_unchanged()

    def test_nine_types_leave_both_coherent_legacy_packets_unchanged(self):
        self.join(9)
        self.run_reports()
        for index in (0, 1):
            self.assertEqual(self.record["drafts"][self.slots[index]["issue_id"]]["revision"], 1)
            self.assertEqual(self.case.memory.snapshot()["issue_reservations"][self.slots[index]["issue_id"]],
                             self.original_holds[self.slots[index]["issue_id"]])
        self.assert_originals_unchanged()

    def test_elsewhere_checked_hold_is_not_stolen_for_upgrade(self):
        ids = self.join(10)
        pool = build_report_pool(self.case.memory, as_of=self.case.as_of, recommendation_contract=RECOMMENDATION_CONTRACT)
        rows = [row for row in pool["eligible"] if row["concept_id"] in ids]
        state = self.case.memory.snapshot()
        allocated = allocate_issues(rows, [{"issue_id": "other-private-draft", "client_id": "default",
            "date": "2026-10-11"}], state["published_history"], state["issue_reservations"], identity_state=state)
        issue = allocated["issues"][0]
        facts = [pool["facts_by_concept"][row["concept_id"]] for row in issue["products"]]
        issue, facts = programme._functional_report_revision(self.case.root, issue, facts, "captured", source_state=state)
        freeze_review_input(self.case.memory, issue, facts, root=self.case.root)
        reserve_issue(self.case.memory, issue, expected_input_hash=issue["input_hash"],
            expected_snapshot_hash=allocation_snapshot_hash(self.case.memory.snapshot()),
            report_root=self.case.root, as_of=self.case.as_of)
        held = deepcopy(self.case.memory.snapshot()["issue_reservations"])
        self.run_reports()
        self.assertEqual(self.case.memory.snapshot()["issue_reservations"], held)
        self.assertEqual([self.input(index)["issue"]["revision"] for index in (0, 1)], [1, 1])
        self.assert_originals_unchanged()

    def test_expired_functional_support_does_not_churn_current_legacy(self):
        older, ids = functional.functional_pool_fixture(self.case.root / "older-genuine-types", count=10,
                                                        at="2026-09-25T10:00:00+00:00")
        self.join(10, source_state=older.snapshot(), source_ids=ids)
        pool = build_report_pool(self.case.memory, as_of=self.case.as_of, recommendation_contract=RECOMMENDATION_CONTRACT)
        self.assertFalse([row for row in pool["eligible"] if row.get("recommendation_contract") == RECOMMENDATION_CONTRACT])
        self.run_reports()
        self.assertEqual([self.input(index)["issue"]["revision"] for index in (0, 1)], [1, 1])
        for ref, hold in self.original_holds.items():
            self.assertEqual(self.case.memory.snapshot()["issue_reservations"][ref], hold)
        self.assert_originals_unchanged()

    def test_uploaded_published_cancelled_issues_are_never_upgraded(self):
        self.join(20)
        for protected in ("uploaded", "published", "cancelled"):
            with self.subTest(protected=protected):
                for index in (0, 1):
                    ref = self.slots[index]["issue_id"]
                    old = self.case.memory.snapshot()["issue_reservations"][ref]
                    self.case.memory.put("issue_reservations", ref, dict(old, state=protected), previous=old)
                before = {self.slots[index]["issue_id"]:
                    deepcopy(self.case.memory.snapshot()["issue_reservations"][self.slots[index]["issue_id"]])
                    for index in (0, 1)}
                result = self.run_reports()
                self.assertTrue(all(any(row["issue_id"] == ref and row["state"] == "protected_issue"
                    for row in result["outcomes"]) for ref in before))
                for ref, hold in before.items():
                    self.assertEqual(self.case.memory.snapshot()["issue_reservations"][ref], hold)
        self.assert_originals_unchanged()

    def test_failed_upgrade_build_keeps_old_owner_and_healthy_retry_upgrades(self):
        self.join(10)
        before = deepcopy(self.record["drafts"][self.slots[0]["issue_id"]])
        with patch.object(programme, "_functional_report_revision", side_effect=SafetyError("FIXTURE_UPGRADE_BUILD_FAILED")):
            result = self.run_reports()
        self.assertTrue(any(row.get("reason") == "FIXTURE_UPGRADE_BUILD_FAILED" for row in result["outcomes"]))
        self.assertEqual(self.record["drafts"][self.slots[0]["issue_id"]], before)
        self.assertEqual(self.case.memory.snapshot()["issue_reservations"][self.slots[0]["issue_id"]],
                         self.original_holds[self.slots[0]["issue_id"]])
        self.assert_originals_unchanged()
        retry = self.record["deferrals"]["report_upgrade:" + self.slots[0]["issue_id"]]
        with patch.object(programme, "_functional_report_revision", wraps=programme._functional_report_revision) as builder:
            self.run_reports()
        self.assertEqual(builder.call_count, 0)
        self.assertEqual(self.record["deferrals"]["report_upgrade:" + self.slots[0]["issue_id"]], retry)
        self.case.as_of = (timestamp(self.case.as_of) + timedelta(seconds=60)).isoformat()
        with patch.object(programme, "_functional_report_revision", wraps=programme._functional_report_revision) as builder:
            self.run_reports()
        self.assertEqual(builder.call_count, 0)
        self.assertEqual(self.record["deferrals"]["report_upgrade:" + self.slots[0]["issue_id"]], retry)
        state = self.case.memory.snapshot()
        ready = programme.programme_turn_readiness(self.case.memory, self.record, root=self.case.root,
            runner=self.case.writer, state=state, as_of=self.case.as_of)
        self.record["cooperative_checkpoint"] = {"version": 1, "continue_ready": False, "wake_hash": ready["wake_hash"]}
        unchanged = programme.programme_turn_readiness(self.case.memory, self.record, root=self.case.root,
            runner=self.case.writer, state=state, as_of=self.case.as_of)
        self.assertFalse(unchanged["ready"], unchanged)
        self.case.as_of = (timestamp(retry["next_retry_at"]) + timedelta(seconds=1)).isoformat()
        due = programme.programme_turn_readiness(self.case.memory, self.record, root=self.case.root,
            runner=self.case.writer, state=state, as_of=self.case.as_of)
        self.assertTrue(due["ready"], due)
        self.run_reports()
        self.assertEqual(self.input(0)["issue"]["revision"], 2)
        self.assertEqual(self.input(0)["issue"]["recommendation_contract"], RECOMMENDATION_CONTRACT)
        self.assertTrue(self.record["drafts"][self.slots[0]["issue_id"]]["complete"])
        self.assertNotIn("report_upgrade:" + self.slots[0]["issue_id"], self.record["deferrals"])
        self.assert_originals_unchanged()

    def test_failed_upgrade_freeze_keeps_old_owner_then_exact_retry_completes(self):
        self.join(10)
        before = deepcopy(self.record["drafts"][self.slots[0]["issue_id"]])
        with patch("product_radar.report_workflow.freeze_review_input", side_effect=OSError("Fictional freeze failure")):
            self.run_reports()
        self.assertEqual(self.record["drafts"][self.slots[0]["issue_id"]], before)
        self.assertEqual(self.case.memory.snapshot()["issue_reservations"][self.slots[0]["issue_id"]],
                         self.original_holds[self.slots[0]["issue_id"]])
        with patch.object(programme, "_functional_report_revision", wraps=programme._functional_report_revision) as builder:
            self.run_reports()
        self.assertEqual(builder.call_count, 0)
        self.case.as_of = (timestamp(self.case.as_of) + timedelta(seconds=301)).isoformat()
        self.run_reports()
        self.assertEqual(self.input(0)["issue"]["revision"], 2)
        self.assertTrue(self.record["drafts"][self.slots[0]["issue_id"]]["complete"])
        self.assert_originals_unchanged()

    def test_canonical_newer_hold_recovers_when_programme_still_points_to_legacy(self):
        self.join(10)
        old = deepcopy(self.record["drafts"][self.slots[0]["issue_id"]])
        self.run_reports()
        new = self.input(0)
        calls, renders = self.case.writer.calls, len(self.case.renders)
        # A genuine checked new hold survives while the programme save is lost.
        self.record["drafts"][self.slots[0]["issue_id"]] = old
        self.run_reports()
        self.assertEqual(self.input(0), new)
        self.assertTrue(self.record["drafts"][self.slots[0]["issue_id"]]["complete"])
        self.assertEqual((self.case.writer.calls, len(self.case.renders)), (calls, renders))
        self.assert_originals_unchanged()

    def test_saved_upgrade_input_before_failed_hold_reuses_same_revision_after_wait(self):
        self.join(10)
        ref = self.slots[0]["issue_id"]
        before = deepcopy(self.record["drafts"][ref])
        calls = self.case.writer.calls
        def reject_new_hold(memory, issue, **options):
            if issue["issue_id"] == ref and issue["revision"] == 2:
                raise SafetyError("FIXTURE_NEW_HOLD_REJECTED")
            return reserve_issue(memory, issue, **options)
        with patch("product_radar.issue_allocation.reserve_issue", side_effect=reject_new_hold):
            self.run_reports()
        saved = load_json(self.case.folder(0, 2) / "package-input.json", 8 * 1024 * 1024)
        self.assertEqual(saved["issue"]["recommendation_contract"], RECOMMENDATION_CONTRACT)
        self.assertEqual(self.record["drafts"][ref], before)
        self.assertEqual(self.case.memory.snapshot()["issue_reservations"][ref], self.original_holds[ref])
        self.assertEqual(self.case.writer.calls, calls)
        retry = deepcopy(self.record["deferrals"]["report_upgrade:" + ref])
        with patch.object(programme, "_functional_report_revision", wraps=programme._functional_report_revision) as builder:
            self.run_reports()
        self.assertEqual(builder.call_count, 0)
        self.assertEqual(self.record["deferrals"]["report_upgrade:" + ref], retry)
        self.case.as_of = (timestamp(self.case.as_of) + timedelta(seconds=301)).isoformat()
        self.run_reports()
        self.assertEqual(self.input(0)["issue"]["revision"], saved["issue"]["revision"])
        self.assertEqual(self.input(0), saved)
        self.assertEqual(self.record["drafts"][ref]["revision"], 2)
        self.assertTrue(self.record["drafts"][ref]["complete"])
        self.assertFalse(self.case.folder(0, 3).exists())
        self.assert_originals_unchanged()

    def test_upgrade_retry_key_ignores_generated_cutoff_but_tracks_source_and_selection_changes(self):
        self.join(10)
        def preview(at):
            state = self.case.memory.snapshot()
            pool = build_report_pool(self.case.memory, as_of=at, recommendation_contract=RECOMMENDATION_CONTRACT)
            eligible = programme._reporting_eligible(self.record, state, pool)
            return programme._functional_report_allocations(eligible, self.slots, state)[0]
        initial = preview(self.case.as_of)
        later = preview((timestamp(self.case.as_of) + timedelta(seconds=60)).isoformat())
        self.assertNotEqual(initial["selection_hash"], later["selection_hash"])
        first = programme._report_upgrade_binding(initial, None)
        self.assertEqual(first, programme._report_upgrade_binding(later, None))
        for field, value in (("date", "2026-10-07"), ("projected_ordinal", 2),
                             ("identity_context_hash", "1" * 64)):
            with self.subTest(field=field):
                changed = deepcopy(initial); changed[field] = value
                self.assertNotEqual(first, programme._report_upgrade_binding(changed, None))
        self.assertNotEqual(first, programme._report_upgrade_binding(initial, "en"))
        reordered = deepcopy(initial); reordered["products"].reverse()
        self.assertNotEqual(first, programme._report_upgrade_binding(reordered, None))
        for field, value in (("source_scope_hash", "1" * 64),
                             ("source_revisions", {"concepts": {"changed-producer": "1" * 64}}),
                             ("opportunity_priority", 4), ("status", "Updated"),
                             ("priority_support", {"valid_until": "2026-10-06T09:00:00+00:00"}),
                             ("supplier_scoped_terms", [{"current": False}]),
                             ("supplier_scope_coverage", {"qualified": 0}),
                             ("source_route_checks", [{"state": "needs_recheck"}])):
            with self.subTest(product_field=field):
                changed = deepcopy(initial); changed["products"][0][field] = value
                self.assertNotEqual(first, programme._report_upgrade_binding(changed, None))

    def test_saved_upgrade_recovery_rejects_changed_current_logo_and_owner(self):
        original, issue, facts, options = self.unheld_upgrade()
        saved = programme._saved_functional_upgrade(self.case.root, issue, facts, **options)
        self.assertEqual(saved, (original["issue"], original["product_facts"]))
        from PIL import Image
        logo = self.case.root / "changed-test-logo.png"
        Image.new("RGB", (4, 4), "#123456").save(logo)
        with patch("product_radar.live_outputs.LOGO", logo):
            self.assertIsNone(programme._saved_functional_upgrade(self.case.root, issue, facts, **options))
        for state in ("uploaded", "published", "cancelled"):
            with self.subTest(owner_state=state):
                altered = deepcopy(options["state"])
                ref = issue["issue_id"]
                altered["issue_reservations"][ref]["state"] = state
                current = dict(options, state=altered, reservation=altered["issue_reservations"][ref])
                self.assertIsNone(programme._saved_functional_upgrade(self.case.root, issue, facts, **current))
                self.assertEqual(programme._current_report_selection(original["issue"], options["eligible"],
                    altered, self.record, expected_replaceable_hold=altered["issue_reservations"][ref]),
                    "REPORT_RESERVATION_CHANGED")
        missing = deepcopy(options["state"]); missing["issue_reservations"].pop(issue["issue_id"])
        self.assertEqual(programme._current_report_selection(original["issue"], options["eligible"],
            missing, self.record, expected_replaceable_hold=options["reservation"]), "REPORT_RESERVATION_CHANGED")
        self.assert_originals_unchanged()

    def test_saved_upgrade_recovery_rejects_actual_unreviewed_source_replacement(self):
        original, issue, facts, options = self.unheld_upgrade()
        selected = issue["products"][0]
        observation = next(options["state"]["observations"][ref]
            for ref in selected["source_revisions"]["observations"]
            if options["state"]["observations"][ref]["source"] == "amazon")
        cards = [dict(amazon_row(), title="New unrelated fictional source listing", asin=observation["source_item"],
            link="https://www.amazon.in/dp/" + observation["source_item"])]
        import_capture(self.case.memory, source="amazon", acquisition_id="upgrade-newer-source-fixture",
            observed_at=self.case.as_of, mapped=map_research_rows(cards, request()), capture_hash=digest(cards),
            reference={"fixture": "synthetic"})
        changed = self.case.memory.snapshot()
        pool = build_report_pool(self.case.memory, as_of=self.case.as_of, recommendation_contract=RECOMMENDATION_CONTRACT)
        eligible = {row["concept_id"]: row for row in programme._reporting_eligible(self.record, changed, pool)}
        self.assertNotEqual(eligible.get(selected["concept_id"]), options["eligible"][selected["concept_id"]])
        self.assertIsNone(programme._saved_functional_upgrade(self.case.root, issue, facts,
            **dict(options, state=changed, eligible=eligible)))
        self.assert_originals_unchanged()

    def test_saved_upgrade_recovery_rejects_actual_selected_alias_review(self):
        original, issue, facts, options = self.unheld_upgrade()
        ids = [row["concept_id"] for row in issue["products"][:2]]
        prepared = prepare_alias_input(self.case.memory.snapshot(), ids, reviewed_at=self.case.as_of)
        accepted = accept_functional_alias(self.case.memory, prepared, reviewer="synthetic_operator",
            method="operator", reason="Explicit fictional operator decision: these two fixture types are equivalent.")
        self.assertTrue(accepted["applied"], accepted)
        changed = self.case.memory.snapshot()
        self.assertEqual(programme._current_report_selection(original["issue"], options["eligible"], changed,
            self.record, expected_replaceable_hold=options["reservation"]), "REPORT_FUNCTIONAL_IDENTITY_CHANGED")
        self.assertIsNone(programme._saved_functional_upgrade(self.case.root, issue, facts,
            **dict(options, state=changed)))
        self.assert_originals_unchanged()

    def test_committed_new_hold_lost_reply_recovers_exact_input_from_stale_programme(self):
        self.join(10)
        ref = self.slots[0]["issue_id"]
        before = deepcopy(self.record["drafts"][ref])
        calls = self.case.writer.calls
        raised = False
        def commit_then_lose_reply(memory, issue, **options):
            nonlocal raised
            result = reserve_issue(memory, issue, **options)
            if issue["issue_id"] == ref and issue["revision"] == 2 and not raised:
                raised = True
                raise OSError("Fictional lost new-hold reply")
            return result
        with patch("product_radar.issue_allocation.reserve_issue", side_effect=commit_then_lose_reply):
            self.run_reports()
        self.assertTrue(raised)
        self.assertEqual(self.case.memory.snapshot()["issue_reservations"][ref]["revision"], 2)
        self.assertEqual(self.case.writer.calls, calls)
        saved = load_json(self.case.folder(0, 2) / "package-input.json", 8 * 1024 * 1024)
        # Emulate the old programme save surviving while the genuine checked
        # new reservation/input survive independently.
        self.record["drafts"][ref] = before
        self.case.as_of = (timestamp(self.case.as_of) + timedelta(seconds=301)).isoformat()
        self.run_reports()
        self.assertEqual(self.input(0), saved)
        self.assertEqual(self.record["drafts"][ref]["revision"], 2)
        self.assertTrue(self.record["drafts"][ref]["complete"])
        self.assertFalse(self.case.folder(0, 3).exists())
        self.assert_originals_unchanged()

    def test_no_functional_cohort_preserves_ordinary_legacy_reuse(self):
        calls, renders = self.case.writer.calls, len(self.case.renders)
        self.run_reports()
        self.assertEqual((self.case.writer.calls, len(self.case.renders)), (calls, renders))
        self.assertEqual([self.input(index)["issue"]["revision"] for index in (0, 1)], [1, 1])
        self.assert_originals_unchanged()

    def test_invalidated_functional_packet_never_downgrades_to_available_legacy_and_recovers(self):
        ids = self.join(10)
        ref = self.slots[0]["issue_id"]
        self.run_reports()
        original = self.case.packet_bytes(0, revision=2)
        owned = deepcopy(self.case.memory.snapshot()["issue_reservations"][ref])
        state = self.case.memory.snapshot()
        meaning = effective_functional_definition(state, ids[0])
        definition = {field: deepcopy(meaning[field]) for field in DEFINITION_FIELDS}
        definition["mechanism"] = "Different explicitly reviewed fictional mechanism"
        semantics = {row["semantic_id"] for row in state["associations"].values()
            if row.get("concept_id") == ids[0]}
        basis = next(ref for ref, row in state["observations"].items() if row["semantic_id"] in semantics)
        accept_functional_definition(self.case.memory, ids[0], definition, basis_refs=[basis],
            reviewer="synthetic_operator", reviewed_at=self.case.as_of, expected_meaning_hash=meaning["meaning_hash"])
        pool = build_report_pool(self.case.memory, as_of=self.case.as_of, recommendation_contract=RECOMMENDATION_CONTRACT)
        current = self.case.memory.snapshot()
        eligible = programme._reporting_eligible(self.record, current, pool)
        self.assertEqual(sum(row.get("recommendation_contract") == RECOMMENDATION_CONTRACT for row in eligible), 9)
        old_first = load_json(self.case.folder(0, 1) / "package-input.json", 8 * 1024 * 1024)["issue"]
        freed_legacy = {row["concept_id"] for row in old_first["products"]}
        self.assertEqual(len(freed_legacy & {row["concept_id"] for row in eligible}), 10)
        calls, renders = self.case.writer.calls, len(self.case.renders)
        result = self.run_reports()
        draft = self.record["drafts"][ref]
        self.assertEqual(draft["revision"], 2)
        self.assertFalse(draft["complete"])
        self.assertEqual(draft["state"], "functional_shortfall")
        self.assertEqual(draft["shortfall"], 1)
        self.assertEqual(draft["candidate_products"], 9)
        self.assertTrue(draft["retained_complete"])
        self.assertEqual(self.case.saved()["drafts"][ref], draft)
        self.assertEqual(self.case.memory.snapshot()["issue_reservations"][ref], owned)
        self.assertEqual(self.case.packet_bytes(0, revision=2), original)
        self.assertFalse(self.case.folder(0, 3).exists())
        self.assertEqual((self.case.writer.calls, len(self.case.renders)), (calls, renders))
        stable = deepcopy(draft)
        deferrals = deepcopy(self.record["deferrals"])
        self.run_reports()
        self.assertEqual(self.record["drafts"][ref], stable)
        self.assertEqual(self.record["deferrals"], deferrals)
        self.assertEqual((self.case.writer.calls, len(self.case.renders)), (calls, renders))
        ready = programme.programme_turn_readiness(self.case.memory, self.record, root=self.case.root,
            runner=None, state=self.case.memory.snapshot(), as_of=self.case.as_of)
        self.record["cooperative_checkpoint"] = {"version": 1, "continue_ready": False, "wake_hash": ready["wake_hash"]}
        unchanged = programme.programme_turn_readiness(self.case.memory, self.record, root=self.case.root,
            runner=None, state=self.case.memory.snapshot(), as_of=self.case.as_of)
        self.assertFalse(unchanged["ready"], unchanged)
        extra = self.functional_ids[10]
        admitted = programme._register_discovered_products(self.case.memory, self.record,
            {"new_concept_ids": [extra], "new_products_need_full_source_review": [extra]}, self.case.as_of)
        self.assertEqual(admitted, [extra])
        due = programme.programme_turn_readiness(self.case.memory, self.record, root=self.case.root,
            runner=None, state=self.case.memory.snapshot(), as_of=self.case.as_of)
        self.assertTrue(due["ready"], due)
        self.run_reports()
        replacement = self.input(0)
        self.assertEqual(replacement["issue"]["revision"], 3)
        self.assertEqual(replacement["issue"]["recommendation_contract"], RECOMMENDATION_CONTRACT)
        self.assertEqual({row["concept_id"] for row in replacement["issue"]["products"]}, set(ids[1:] + [extra]))
        self.assertTrue(self.record["drafts"][ref]["complete"])
        self.assertEqual(self.case.packet_bytes(0, revision=2), original)
        self.assert_originals_unchanged()


class ProgrammeFunctionalShortageHonestyTests(unittest.TestCase):
    def test_uninspected_preview_does_not_invent_a_functional_shortage(self):
        # A different orphan packet waiting for its checked hold blocks ordinal
        # allocation. That is not a zero-result functional candidate search.
        state = {"issue_reservations": {}, "published_history": {}}
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        memory = SimpleNamespace(root=root, snapshot=lambda: deepcopy(state), evidence_type="fixture")
        slots = [{"issue_id": ref, "client_id": "default", "date": date}
                 for ref, date in (("functional-old", "2026-10-08"), ("orphan-old", "2026-10-10"))]
        original = {"issue_id": "functional-old", "revision": 2, "complete": True,
                    "state": "ready_for_review", "input_hash": "original-checked-input"}
        record = {"release_slots": slots, "drafts": {"functional-old": deepcopy(original)}}
        def saved(root, slot, draft, reservation, evidence_type):
            issue = dict(slot, revision=2, products=[], input_hash="checked-fixture")
            if slot["issue_id"] == "functional-old":
                issue["recommendation_contract"] = RECOMMENDATION_CONTRACT
            return {"issue": issue, "product_facts": []}
        def current(issue, eligible, source_state, saved_record):
            return "REPORT_CURRENT_ELIGIBILITY_CHANGED" if issue["issue_id"] == "functional-old" else None
        with patch.object(programme, "_programme_report_pool", return_value={}), \
                patch.object(programme, "_reporting_eligible", return_value=[]), \
                patch.object(programme, "_frozen_report_input", side_effect=saved), \
                patch.object(programme, "_current_report_selection", side_effect=current), \
                patch.object(programme, "_functional_report_allocations") as preview, \
                patch("product_radar.live_outputs.build_issue_dataset", return_value={}), \
                patch("product_radar.issue_allocation.allocation_snapshot_hash", return_value="fixture-state"), \
                patch("product_radar.issue_allocation.reserve_issue", side_effect=SafetyError("FIXTURE_ADOPTION_WAIT")), \
                patch("product_radar.issue_allocation.allocate_issues") as fallback:
            result = programme._reports(memory, record, {"root": root, "at": functional.NOW})
        preview.assert_not_called()
        fallback.assert_not_called()
        self.assertEqual(record["drafts"]["functional-old"], original)
        self.assertEqual([row["issue_id"] for row in result["outcomes"]], ["orphan-old"])
        self.assertEqual(result["outcomes"][0]["reason"], "FIXTURE_ADOPTION_WAIT")


if __name__ == "__main__":
    unittest.main()
