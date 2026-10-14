"""The R10 cutover helper on synthetic saved records; no provider, website or model."""

import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from product_radar import r10_cutover as cutover
from product_radar.inventory_intake import INTAKE_KIND, current_intake_records
from product_radar.meta_facts import FACTS_VERSION
from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory, key
from product_radar.storage import Journal
import r10_cutover as command

NOW = "2026-10-20T10:00:00+00:00"
BEFORE = "2026-10-14T10:00:00+00:00"
LATER = "2026-10-21T10:00:00+00:00"


def witness(*sources):
    return {"contract": "functional-priority-support-v1", "sources": [{"source": source} for source in sources]}


class CutoverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="r10-cutover-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in cutover.CONFIG_FILES[:-1]:  # the reviewed Meta request file is missing
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / name, self.root / name)
        self.memory = ResearchMemory(self.root / "captured", Journal(self.root / "finance"), evidence_type="synthetic")
        self.accounts = []

    def intake(self, concept_id, support, admitted_at=BEFORE):
        binding = {"client_id": "default", "concept_id": concept_id, "meaning_hash": "a" * 64, "eligibility_hash": "b" * 64,
                   "status": "New", "support_dependencies": {}, "priority_support": support, "usable_until": None}
        ref = key("reporting-intake", binding)
        self.memory.put("batch_runs", ref, {"id": ref, "kind": INTAKE_KIND, "binding": binding,
                                            "binding_hash": digest(binding), "admitted_at": admitted_at})
        return ref

    def issue(self, issue_id, state, product, reserved_at=BEFORE, **extra):
        self.memory.put("issue_reservations", issue_id, {"issue_id": issue_id, "state": state, "revision": 1,
            "selection_hash": "s-" + issue_id, "reserved_at": reserved_at, "products": [product], **extra})

    def task(self, task_id, route, status, **extra):
        self.memory.put("query_tasks", task_id, {"kind": "all_source_task_v1", "route": route, "status": status,
                                                  "targets": {}, **extra})

    def enable(self, root):
        self.accounts.append(root)
        return {"written": len(self.accounts) == 1, "path": cutover.ACCOUNT_LIST}

    def fixture(self):
        old = self.intake("meta-type", witness("amazon", "meta"))
        self.intake("amazon-type", witness("amazon"))
        self.intake("unsupported-type", None)
        old_meta = {"concept_id": "x", "market": {"sources": {"meta": {"used_ids": ["ad"]}}}}
        current_meta = {"concept_id": "y", "market": {"sources": {"meta": {"depth": {"version": FACTS_VERSION}}}}}
        self.issue("issue-old-draft", "reserved", old_meta)
        self.issue("issue-witness-draft", "reserved", {"concept_id": "z", "priority_support": witness("meta")})
        self.issue("issue-current-draft", "reserved", current_meta)
        self.issue("issue-old-uploaded", "uploaded", old_meta, activation_receipt={"remote_state": "active"})
        self.issue("issue-cancelled", "cancelled", old_meta)
        self.task("trends-old-wait", "google-trends", "failed", next_retry_at=LATER)
        self.task("trends-signed-in-wait", "google-trends", "prepared", next_retry_at=LATER,
                  source_physical_request={"transport": "signed-in-browser"})
        self.task("movers-open", "amazon-movers-and-shakers", "prepared")
        self.task("movers-done", "amazon-movers-and-shakers", "completed_no_results")
        self.memory.put("batch_runs", "movers-owner", {"kind": "source-acquisition-owner-v1",
                                                       "binding": {"route": "amazon-movers-and-shakers"}})
        return old

    def test_check_reads_only_and_names_everything_the_cutover_finds(self):
        old = self.fixture()
        events = sorted((self.memory.root / "events").glob("*.json"))
        found = cutover.cutover_check(self.memory, root=self.root, as_of=NOW)
        self.assertEqual(sorted((self.memory.root / "events").glob("*.json")), events)
        self.assertEqual(found["old_meta_intake_ids"], [old])
        self.assertEqual(found["old_meta_reserved_issue_ids"], ["issue-old-draft", "issue-witness-draft"])
        self.assertEqual(found["old_meta_published_issue_ids_need_owner"], ["issue-old-uploaded"])
        self.assertEqual(found["old_identity_trends_waits"], ["trends-old-wait"])
        self.assertEqual(found["retired_route_open_tasks"], {"amazon-movers-and-shakers": ["movers-open"]})
        self.assertEqual(found["retired_route_saved_records"], {"amazon-movers-and-shakers": 3})
        self.assertEqual(found["missing_config_files"], ["config/benchmarks/meta_first.json"])
        self.assertFalse(found["account_list_present"])
        self.assertIsNone(found["marker"])

    def test_apply_changes_each_item_once_with_its_reason_and_repeats_nothing(self):
        old = self.fixture()
        result, ok = cutover.apply_cutover(self.memory, root=self.root, as_of=NOW, enable_accounts=self.enable)
        self.assertTrue(ok)
        self.assertEqual(result["account_list"]["written"], True)
        self.assertEqual(result["trends_waits_released"], ["trends-old-wait"])
        self.assertEqual(result["intake_expired"], [old])
        self.assertEqual(result["issues_cancelled"], ["issue-old-draft", "issue-witness-draft"])
        self.assertEqual(result["published_issues_need_owner"], ["issue-old-uploaded"])
        self.assertTrue(result["marker_saved"])
        state = self.memory.snapshot()
        self.assertEqual(state["batch_runs"][old]["expired"], {"at": NOW, "reason": cutover.REASON})
        self.assertNotIn(old, [row["id"] for row in current_intake_records(
            state, {"client_id": "default", "reporting_intake_ids": [old]})])
        for issue_id in ("issue-old-draft", "issue-witness-draft"):
            self.assertEqual((state["issue_reservations"][issue_id]["state"], state["issue_reservations"][issue_id]["cancel_reason"]),
                             ("cancelled", cutover.REASON))
        self.assertEqual(state["issue_reservations"]["issue-current-draft"]["state"], "reserved")
        self.assertEqual(state["issue_reservations"]["issue-old-uploaded"]["state"], "uploaded")
        self.assertIsNone(state["query_tasks"]["trends-old-wait"]["next_retry_at"])
        self.assertEqual(state["query_tasks"]["trends-signed-in-wait"]["next_retry_at"], LATER)
        self.assertEqual(cutover.marker(state)["applied_at"], NOW)
        # Records saved after the cutover are the current rule's: never touched.
        newer = self.intake("new-meta-type", witness("meta"), admitted_at=LATER)
        events = len(list((self.memory.root / "events").glob("*.json")))
        again, ok = cutover.apply_cutover(self.memory, root=self.root, as_of=LATER, enable_accounts=self.enable)
        self.assertTrue(ok)
        self.assertEqual((again["trends_waits_released"], again["intake_expired"], again["issues_cancelled"], again["marker_saved"]),
                         ([], [], [], False))
        self.assertEqual(len(list((self.memory.root / "events").glob("*.json"))), events)
        self.assertNotIn("expired", self.memory.snapshot()["batch_runs"][newer])
        self.assertEqual(cutover.cutover_check(self.memory, root=self.root, as_of=LATER)["old_meta_intake_ids"], [])

    def test_a_failed_account_list_is_reported_without_hiding_the_other_steps(self):
        old = self.fixture()
        def refuse(root):
            raise SafetyError("ACCOUNT_FILES_REQUIRED")
        result, ok = cutover.apply_cutover(self.memory, root=self.root, as_of=NOW, enable_accounts=refuse)
        self.assertFalse(ok)
        self.assertEqual(result["account_list"], {"written": False, "reason": "ACCOUNT_FILES_REQUIRED"})
        self.assertEqual(result["intake_expired"], [old])

    def test_command_prints_check_and_apply_results(self):
        self.fixture()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(command.main(["check"], root=self.root, memory=self.memory), 0)
        self.assertEqual(json.loads(output.getvalue())["old_identity_trends_waits"], ["trends-old-wait"])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(command.main(["apply"], root=self.root, memory=self.memory, enable_accounts=self.enable), 0)
        self.assertEqual(json.loads(output.getvalue())["trends_waits_released"], ["trends-old-wait"])
        output = io.StringIO()
        with redirect_stdout(output):  # the real account list needs the private files: a clear failure
            self.assertEqual(command.main(["apply"], root=self.root, memory=self.memory), 1)
        self.assertEqual(json.loads(output.getvalue())["account_list"]["reason"], "ACCOUNT_FILES_REQUIRED")
        from unittest.mock import patch
        output = io.StringIO()
        with redirect_stdout(output), patch.object(self.memory, "snapshot", side_effect=SafetyError("RESEARCH_EVENTS_INVALID")):
            self.assertEqual(command.main(["check"], root=self.root, memory=self.memory), 1)
        self.assertEqual(json.loads(output.getvalue()), {"state": "stopped", "reason": "RESEARCH_EVENTS_INVALID"})

if __name__ == "__main__":
    unittest.main()
