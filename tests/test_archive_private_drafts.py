"""Older private-draft archive: synthetic holds and files only; no provider calls."""

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import archive_private_drafts as script
from product_radar.issue_allocation import cancel_unpublished_issues
from product_radar.models import SafetyError
from product_radar.programme import prepare_programme
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, safe_path

NOW = "2026-10-13T16:00:00+00:00"
OLD = ["issue-default-2026-10-06", "issue-default-2026-10-08", "issue-default-2026-10-10"]


def hold(issue_id, *, contract=None, state="reserved", **extra):
    position = OLD.index(issue_id) + 1 if issue_id in OLD else 1  # Saved positions 1, 2, 3 as in real holds.
    row = {"issue_id": issue_id, "client_id": "default", "date": issue_id[-10:], "revision": 2, "state": state,
           "products": [{"concept_id": issue_id + "-p" + str(n)} for n in range(10)],
           "selection_hash": "selection-" + issue_id, "projected_ordinal": position, **extra}
    if contract:
        row["recommendation_contract"] = contract
    return row


class ArchivePrivateDraftsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        for issue_id in OLD:
            self.memory.put("issue_reservations", issue_id, hold(issue_id))
        prepare_programme(self.memory, "older-week", concept_ids=[], as_of=NOW, week="2026-10-05")
        prepare_programme(self.memory, "current-week", concept_ids=[], as_of=NOW, week="2026-10-12")
        for issue_id in OLD:
            folder = self.root / "data/outputs" / issue_id / "revision-1"
            folder.mkdir(parents=True)
            (folder / "report-en.pdf").write_bytes(b"synthetic archived draft " + issue_id.encode())

    def run_script(self, *arguments):
        out = io.StringIO()
        with redirect_stdout(out):
            code = script._main([*arguments], memory=self.memory, root=self.root, clock=lambda: NOW)
        return code, json.loads(out.getvalue())

    def ids(self, values=OLD):
        return [part for issue_id in values for part in ("--issue-id", issue_id)]

    def slots(self, rows):
        return {row["issue_id"]: row for row in rows}

    def test_dry_run_reports_projection_and_changes_nothing(self):
        before = self.memory.snapshot_with_provenance()
        code, output = self.run_script(*self.ids())
        self.assertEqual(code, 0)
        self.assertEqual((output["mode"], output["eligible"], output["changed"]), ("dry_run", True, False))
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        self.assertEqual([row["owning_programme_id"] for row in output["drafts"]], ["older-week"] * 3)
        now, after = self.slots(output["projection_before"]), self.slots(output["projection_after_if_executed"])
        self.assertEqual(now["issue-default-2026-10-13"]["if_next"], {"ordinal": 4, "language_state": "language_pending"})
        self.assertEqual([now[ref]["if_all_open_produced"]["ordinal"] for ref in
                          ("issue-default-2026-10-13", "issue-default-2026-10-15", "issue-default-2026-10-17")], [4, 5, 6])
        self.assertEqual([after[ref]["if_all_open_produced"] for ref in ("issue-default-2026-10-13", "issue-default-2026-10-15")],
                         [{"ordinal": 1, "language_state": "bilingual_trial"}, {"ordinal": 2, "language_state": "bilingual_trial"}])
        self.assertEqual(after["issue-default-2026-10-15"]["if_next"]["ordinal"], 1)
        self.assertEqual(after["issue-default-2026-10-06"]["reservation_state"], "cancelled")
        self.assertIn("issue-default-2026-10-20", now)  # Following week's dates are shown too.
        self.assertEqual(output["report_files"]["files"], 3)

    def test_execute_cancels_all_together_with_reason_and_keeps_files(self):
        files = {path: path.read_bytes() for path in (self.root / "data/outputs").rglob("*") if path.is_file()}
        events = len(list((self.memory.root / "events").glob("*.json")))
        code, output = self.run_script(*self.ids(), "--execute", "--reason", "Owner archive of old drafts.")
        self.assertEqual(code, 0, output)
        self.assertEqual(output["cancelled"], dict.fromkeys(OLD, "cancelled"))
        self.assertEqual(len(list((self.memory.root / "events").glob("*.json"))), events + 1)
        state = self.memory.snapshot()
        for issue_id in OLD:
            row = state["issue_reservations"][issue_id]
            self.assertEqual((row["cancel_reason"], row["cancelled_at"]), ("Owner archive of old drafts.", NOW))
            self.assertEqual(row["products"], hold(issue_id)["products"])
        self.assertEqual(state["published_history"], {})
        self.assertEqual({path: path.read_bytes() for path in files}, files)
        after = self.slots(output["projection_after"])
        self.assertEqual(after["issue-default-2026-10-13"]["if_next"]["ordinal"], 1)
        self.assertIsNone(after["issue-default-2026-10-08"]["ordinal"])

    def test_any_refused_draft_changes_nothing(self):
        cases = {"current-format": hold("issue-default-2026-10-10", contract="functional-type-examples-v1"),
                 "activation": hold("issue-default-2026-10-10", activation_receipt={"verified": True}),
                 "uploaded": hold("issue-default-2026-10-10", state="uploaded")}
        for name, row in cases.items():
            with self.subTest(name=name):
                old = self.memory.snapshot()["issue_reservations"]["issue-default-2026-10-10"]
                self.memory.put("issue_reservations", "issue-default-2026-10-10", row, previous=old)
                before = self.memory.snapshot_with_provenance()
                self.assertEqual(self.run_script(*self.ids())[0], 1)
                code, output = self.run_script(*self.ids(), "--execute")
                self.assertEqual((code, output["changed"]), (1, False))
                self.assertEqual(self.memory.snapshot_with_provenance(), before)
                self.memory.put("issue_reservations", "issue-default-2026-10-10", hold("issue-default-2026-10-10"),
                                previous=row, event_id="synthetic-restore-" + name)

    def test_published_history_or_missing_hold_is_refused(self):
        self.memory.put("published_history", "synthetic-publication", {"status": "published", "client_id": "default",
            "issue_id": "issue-default-2026-10-08", "concept_id": "issue-default-2026-10-08-p0"})
        code, output = self.run_script(*self.ids(), "--issue-id", "issue-default-2026-09-29", "--execute")
        self.assertEqual(code, 1)
        refusals = {row["issue_id"]: row["refusals"] for row in output["drafts"]}
        self.assertEqual(refusals["issue-default-2026-10-08"], ["ISSUE_PUBLISHED_HISTORY_PRESENT"])
        self.assertEqual(refusals["issue-default-2026-09-29"], ["ISSUE_RESERVATION_MISSING"])
        self.assertTrue(all(row["state"] == "reserved" for row in self.memory.snapshot()["issue_reservations"].values()))

    def test_execute_fails_fast_while_programme_writer_is_held(self):
        before = self.memory.snapshot_with_provenance()
        with Journal(safe_path(self.memory.root, "programme_writer")).locked():
            code, output = self.run_script(*self.ids(), "--execute")
        self.assertEqual((code, output["state"], output["reason"], output["changed"]),
                         (1, "refused", "RUNNER_ALREADY_ACTIVE", False))
        self.assertEqual(self.memory.snapshot_with_provenance(), before)

    def test_execute_warms_the_verified_read_before_taking_the_writer_guard(self):
        # A cold replay of the live store takes minutes; it must not run while
        # this script holds the guard that every research turn needs.
        guard_states, original = [], self.memory.snapshot
        def observed():
            try:
                with Journal(safe_path(self.memory.root, "programme_writer")).locked():
                    guard_states.append("free")
            except SafetyError as error:
                guard_states.append(str(error))
            return original()
        self.memory.snapshot = observed
        try:
            code, output = self.run_script(*self.ids(), "--execute")
        finally:
            del self.memory.snapshot
        self.assertEqual((code, output["cancelled"]), (0, dict.fromkeys(OLD, "cancelled")))
        self.assertEqual(guard_states[0], "free")
        self.assertIn("RUNNER_ALREADY_ACTIVE", guard_states[1:])  # The decisive read is still under the guard.

    def test_dry_run_ignores_a_newer_staged_checkpoint_from_a_live_writer(self):
        from product_radar.models import digest
        from product_radar.programme import _control_path
        path = _control_path(self.memory, "current-week")
        staged = json.loads(path.read_text())
        newer = dict(staged["record"], cycles=7)  # Saved after this read began.
        path.write_text(json.dumps({"record": newer, "record_hash": digest(newer), "previous_hash": "f" * 64}))
        code, output = self.run_script(*self.ids())
        self.assertEqual((code, output["eligible"]), (0, True))
        self.assertIn("issue-default-2026-10-13", self.slots(output["projection_before"]))

    def test_dry_run_takes_no_writer_lock(self):
        with Journal(safe_path(self.memory.root, "programme_writer")).locked():
            code, output = self.run_script(*self.ids())
        self.assertEqual((code, output["mode"]), (0, "dry_run"))

    def test_bad_reason_or_identifier_is_refused(self):
        for arguments in (("--reason", " "), ("--reason", "x" * 501), ("--issue-id", "../escape")):
            with self.subTest(arguments=arguments):
                code, output = self.run_script(*self.ids(), *arguments, "--execute")
                self.assertEqual((code, output["changed"]), (1, False))

    def test_report_check_failure_after_commit_is_reported_honestly(self):
        calls = []
        def fingerprint(root, issue_ids):
            calls.append(1)
            return {"files": len(calls)}
        original = script.report_file_fingerprint
        script.report_file_fingerprint = fingerprint
        try:
            code, output = self.run_script(*self.ids(), "--execute")
        finally:
            script.report_file_fingerprint = original
        self.assertEqual((code, output["state"], output["reason"], output["changed"]),
                         (1, "archived_with_check_failure", "ARCHIVE_REPORT_FILES_CHANGED", True))


class CancelUnpublishedIssuesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
        for issue_id in OLD:
            self.memory.put("issue_reservations", issue_id, hold(issue_id))
        self.expected = {issue_id: "selection-" + issue_id for issue_id in OLD}

    def test_changed_selection_or_bad_inputs_change_nothing(self):
        before = self.memory.snapshot_with_provenance()
        for expected, reason, code in (
                (dict(self.expected, **{OLD[2]: "older-selection"}), "Archive.", "ISSUE_CANCEL_SELECTION_CHANGED"),
                (self.expected, "", "ISSUE_CANCEL_REASON_INVALID"), ({}, "Archive.", "ISSUE_CANCEL_SELECTION_INVALID"),
                ({OLD[0]: 1}, "Archive.", "ISSUE_CANCEL_SELECTION_INVALID"),
                ({"issue-default-2026-09-29": "x"}, "Archive.", "ISSUE_RESERVATION_MISSING")):
            with self.subTest(code=code), self.assertRaisesRegex(SafetyError, code):
                cancel_unpublished_issues(self.memory, expected, as_of=NOW, reason=reason)
        self.assertEqual(self.memory.snapshot_with_provenance(), before)

    def test_activated_or_published_draft_is_refused_inside_the_lock(self):
        old = self.memory.snapshot()["issue_reservations"][OLD[1]]
        self.memory.put("issue_reservations", OLD[1], dict(old, activation_receipt={"verified": True}), previous=old)
        before = self.memory.snapshot_with_provenance()
        with self.assertRaisesRegex(SafetyError, "ISSUE_CANCEL_NOT_PRIVATE_DRAFT"):
            cancel_unpublished_issues(self.memory, self.expected, as_of=NOW, reason="Archive.")
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        self.memory.put("issue_reservations", OLD[1], old, previous=self.memory.snapshot()["issue_reservations"][OLD[1]],
                        event_id="synthetic-restore-private-draft")
        self.memory.put("published_history", "synthetic-publication", {"status": "published", "client_id": "default",
            "issue_id": OLD[2], "concept_id": OLD[2] + "-p0"})
        before = self.memory.snapshot_with_provenance()
        with self.assertRaisesRegex(SafetyError, "ISSUE_CANCEL_PUBLISHED_HISTORY"):
            cancel_unpublished_issues(self.memory, self.expected, as_of=NOW, reason="Archive.")
        self.assertEqual(self.memory.snapshot_with_provenance(), before)

    def test_second_archive_of_cancelled_drafts_is_refused(self):
        values = cancel_unpublished_issues(self.memory, self.expected, as_of=NOW, reason="Archive.")
        self.assertEqual(sorted(values), OLD)
        with self.assertRaisesRegex(SafetyError, "ISSUE_CANCEL_NOT_PRIVATE_DRAFT"):
            cancel_unpublished_issues(self.memory, deepcopy(self.expected), as_of=NOW, reason="Archive.")


if __name__ == "__main__":
    unittest.main()
