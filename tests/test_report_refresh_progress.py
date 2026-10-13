"""Real report refresh beside unrelated continuing research; no live calls."""

from copy import deepcopy
import unittest

import test_programme_reports as fixtures
from product_radar.models import digest
from product_radar.programme import _programme_report_pool, _reporting_eligible
from product_radar.storage import load_json


class ReportRefreshProgressTests(unittest.TestCase):
    def setUp(self):
        case = fixtures.ProgrammeReportsTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.case = case

    def assert_original_unchanged(self, original):
        self.assertTrue(all(self.case.root.joinpath(path).read_bytes() == raw
                            for path, raw in original.items()))

    def selected_current_inputs(self, ids):
        case = self.case
        record = case.saved()
        state = case.memory.snapshot()
        pool = _programme_report_pool(case.memory, record, as_of=case.as_of)
        eligible = {row["concept_id"]: row for row in _reporting_eligible(record, state, pool)}
        return digest([(cid, eligible.get(cid), pool["facts_by_concept"].get(cid)) for cid in ids])

    def test_changed_selected_inputs_finish_without_waiting_for_unrelated_research(self):
        case = self.case
        case.seed(20, ready=10)
        case.advance(max_cycles=1)
        original = case.packet_bytes()
        frozen = load_json(case.folder() / "package-input.json", 8 * 1024 * 1024)
        selected = [row["concept_id"] for row in frozen["issue"]["products"]]
        first_id = case.slots[0]["issue_id"]
        progress, changed, hashes = [], [], []

        def unrelated_research(*_args):
            # Selected facts change once. Every following round keeps doing
            # actual canonical work, but only on unrelated observations.
            if not changed:
                case.refresh_selected()
                case.accept_pending()
                changed.append(True)
            case.unrelated_progress(progress)
            return {"state": "synthetic_unrelated_research_continues"}

        case.advance(steps=case.steps(unrelated_research), max_cycles=1)
        current = case.saved()["drafts"][first_id]
        self.assertTrue(current["complete"])
        self.assertEqual(current["revision"], 2)
        self.assertEqual(case.memory.snapshot()["issue_reservations"][first_id]["revision"], 2)
        self.assertTrue(case.saved()["drafts"][case.slots[1]["issue_id"]]["complete"])
        hashes.append(self.selected_current_inputs(selected))
        writing, renders = case.writer.calls, len(case.renders)
        replacement = case.packet_bytes(revision=2)

        for _ in range(4):
            case.advance(steps=case.steps(unrelated_research), max_cycles=1)
            hashes.append(self.selected_current_inputs(selected))
            current = case.saved()["drafts"][first_id]
            self.assertTrue(current["complete"])
            self.assertEqual(current["revision"], 2)

        self.assertEqual(len(progress), 5)
        self.assertEqual(len(set(hashes)), 1)
        self.assertEqual((case.writer.calls, len(case.renders)), (writing, renders))
        self.assertFalse(case.folder(revision=3).exists())
        self.assert_original_unchanged(original)
        self.assert_original_unchanged(replacement)
        self.assertEqual(case.memory.snapshot()["published_history"], {})

    def protected_state_stays_protected_during_productive_research(self, protected_state):
        case = self.case
        case.seed()
        case.advance(max_cycles=1)
        first_id = case.slots[0]["issue_id"]
        original = case.packet_bytes()
        original_hold = case.memory.snapshot()["issue_reservations"][first_id]
        protected = dict(original_hold, state=protected_state)
        case.memory.put("issue_reservations", first_id, protected, previous=original_hold)
        case.refresh_selected()
        writing, renders = case.writer.calls, len(case.renders)
        progress = []

        for _ in range(3):
            case.advance(steps=case.steps(lambda *_args: case.unrelated_progress(progress) or {}), max_cycles=1)
            draft = case.saved()["drafts"][first_id]
            self.assertEqual(draft["state"], "protected_issue")
            self.assertEqual(draft["reason"], "REPORT_RESERVATION_PROTECTED")
            self.assertFalse(draft["complete"])
            self.assertTrue(draft["retained_complete"])

        self.assertEqual(len(progress), 3)
        self.assertEqual(case.memory.snapshot()["issue_reservations"][first_id], protected)
        self.assertFalse(case.folder(revision=2).exists())
        self.assertTrue(all(first_id not in path for path in case.renders[renders:]))
        if protected_state != "cancelled":
            self.assertEqual((case.writer.calls, len(case.renders)), (writing, renders))
        # A cancelled issue releases its holds, so another logical issue may
        # progress; cancellation must not reactivate the original issue.
        self.assert_original_unchanged(original)
        self.assertEqual(case.memory.snapshot()["published_history"], {})

    def test_uploaded_issue_is_not_replaced_during_productive_research(self):
        self.protected_state_stays_protected_during_productive_research("uploaded")

    def test_published_issue_is_not_replaced_during_productive_research(self):
        self.protected_state_stays_protected_during_productive_research("published")

    def test_cancelled_issue_is_not_reactivated_during_productive_research(self):
        self.protected_state_stays_protected_during_productive_research("cancelled")

    def test_conflicting_saved_reservation_is_not_overwritten_by_productive_research(self):
        case = self.case
        case.seed()
        case.advance(max_cycles=1)
        first_id = case.slots[0]["issue_id"]
        original = case.packet_bytes()
        original_hold = case.memory.snapshot()["issue_reservations"][first_id]
        changed = deepcopy(original_hold)
        changed.update(revision=2, selection_hash="f" * 64)
        case.memory.put("issue_reservations", first_id, changed, previous=original_hold)
        writing, renders, progress = case.writer.calls, len(case.renders), []

        for _ in range(3):
            case.advance(steps=case.steps(lambda *_args: case.unrelated_progress(progress) or {}), max_cycles=1)
            draft = case.saved()["drafts"][first_id]
            self.assertFalse(draft["complete"])
            self.assertEqual(draft["reason"], "REPORT_RESERVATION_CHANGED")
            self.assertEqual(case.memory.snapshot()["issue_reservations"][first_id], changed)

        self.assertEqual(len(progress), 3)
        self.assertEqual((case.writer.calls, len(case.renders)), (writing, renders))
        self.assertFalse(case.folder(revision=2).exists())
        self.assert_original_unchanged(original)
        self.assertEqual(case.memory.snapshot()["published_history"], {})


if __name__ == "__main__":
    unittest.main()
