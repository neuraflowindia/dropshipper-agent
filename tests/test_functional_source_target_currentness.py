"""Selected-target currentness with real, isolated synthetic packet producers.

No provider, hosted website, release gate or live research state is used.
Original record capsules remain raw; only currentness ignores foreign targets.
"""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import tempfile
import unittest

from product_radar.frozen_records import read_original_json
from product_radar.issue_allocation import allocation_snapshot_hash, reserve_issue
from product_radar.models import SafetyError, digest
from product_radar.programme import _current_report_selection
from product_radar.report_examples import RECOMMENDATION_CONTRACT
from product_radar.report_narration import generate_report_narratives, make_narrative_input
from product_radar.report_selection import build_report_pool, selection_scope
from product_radar.report_source_facts import (
    CONTRACT, FUNCTIONAL_CONTRACT, functional_task_projection, route_dependencies,
    source_revisions_current, validate_functional_route_proof, _functional_route_state,
)
from product_radar.report_workflow import freeze_review_input
from product_radar.research_memory import ResearchMemory, change
from product_radar.source_coverage import (
    CHECK_KIND, RELEVANCE_KIND, bind_source_relevance, execute_source_run, prepare_source_tasks,
)
from product_radar.storage import Journal
from test_fit_pipeline import WritingFixture
from test_functional_report_packet import functional_packet_fixture
from test_functional_source_market import functional_pool_fixture, NOW


class FunctionalSourceTargetCurrentnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.memory, ids = functional_pool_fixture(cls.root / "inputs", count=10)
        cls.cid, cls.foreign = ids[0], "later-unselected-type"
        planned = prepare_source_tasks(cls.memory, [{"route": "amazon-bestsellers",
            "request": {"department": "kitchen"}, "concept_ids": [cls.cid]}],
            run_id="target-currentness", as_of=NOW)
        cls.task_id = planned["task_ids"][0]

        def synthetic_reply(context):
            context["record_dispatch"]()
            return {"schema_version": 1, "route": context["route"],
                "acquisition_id": context["acquisition_id"], "observed_at": NOW,
                "state": "usable", "dispatch_started": True,
                "reference": context["acquisition_id"] + "/synthetic.json", "capture_ids": [],
                "observation_ids": [], "target_results": [], "reason": "Synthetic saved response."}

        execute_source_run(cls.memory, "target-currentness", as_of=NOW,
            handlers={"amazon-bestsellers": synthetic_reply},
            bounded_call=lambda callback, context, timeout: callback(context))
        cls.memory, cls.pool, cls.issue, cls.dataset = functional_packet_fixture(cls.root, memory=cls.memory)
        cls.facts = [cls.pool["facts_by_concept"][row["concept_id"]] for row in cls.issue["products"]]
        freeze_review_input(cls.memory, cls.issue, cls.facts, root=cls.root)
        cls.baseline = cls.memory.snapshot()
        cls.original_input = (cls.root / "data/outputs/functional-test-issue/revision-1/package-input.json").read_bytes()

    def setUp(self):
        self.state = deepcopy(self.baseline)
        self.product = deepcopy(next(row for row in self.issue["products"] if row["concept_id"] == self.cid))
        self.task = self.state["query_tasks"][self.task_id]
        self.record = {"release_slots": [{"issue_id": self.issue["issue_id"], "date": self.issue["date"],
                                         "client_id": self.issue["client_id"]}]}

    def add_foreign(self, state=None):
        state = self.state if state is None else state
        state["concepts"][self.foreign] = {"description": deepcopy(state["concepts"][self.cid]["description"]),
                                           "status": "provisional"}
        state["query_tasks"][self.task_id]["targets"][self.foreign] = digest(state["concepts"][self.foreign]["description"])
        state["query_tasks"][self.task_id]["revision"] += 1
        return state

    def add_foreign_maps(self):
        self.add_foreign()
        self.task["query_plans"] = {self.foreign: {"profile_hash": "1" * 64, "term_index": 1}}
        self.task["target_results"][self.foreign] = {"description_hash": self.task["targets"][self.foreign],
            "state": "insufficient", "reason": "Synthetic foreign interpretation.", "evidence_refs": []}
        self.task["superseded_targets"] = {self.foreign: {"task_id": "foreign-replacement", "reason": "Foreign only."}}

    def foreign_relevance(self):
        return {"kind": RELEVANCE_KIND, "source": "amazon", "route": "amazon-bestsellers", "at": NOW,
            "state": "reviewed", "reason": "Synthetic foreign interpretation.", "reference": "foreign-review",
            "task_id": self.task_id, "receipt_refs": list(self.task["receipt_refs"]),
            "results": [{"concept_id": self.foreign, "description_hash": self.task["targets"][self.foreign],
                "state": "insufficient", "evidence_refs": list(self.task["receipt_refs"]), "reason": "Foreign only."}]}

    def eligible(self):
        view = SimpleNamespace(root=self.memory.root, evidence_type="synthetic", snapshot=lambda: deepcopy(self.state))
        pool = build_report_pool(view, as_of=NOW, recommendation_contract=RECOMMENDATION_CONTRACT)
        return {row["concept_id"]: row for row in pool["eligible"]}

    def clone_memory(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        memory = ResearchMemory(base / "research", Journal(base / "finance"), evidence_type="synthetic")
        memory.commit("synthetic-currentness-seed", [change(table, rid, row)
            for table, rows in self.state.items() for rid, row in rows.items()])
        return memory

    def reserve(self, memory):
        return reserve_issue(memory, self.issue, expected_input_hash=self.issue["input_hash"],
            expected_snapshot_hash=allocation_snapshot_hash(memory.snapshot()), as_of=NOW, report_root=self.root)

    def test_foreign_target_and_optional_maps_do_not_change_projection(self):
        before = functional_task_projection(self.task, self.cid)
        self.add_foreign_maps()
        self.assertEqual(before, functional_task_projection(self.task, self.cid))

    def test_selected_maps_and_all_physical_unknown_fields_remain_dependencies(self):
        before = functional_task_projection(self.task, self.cid)
        mutations = {
            "targets": {self.cid: "f" * 64},
            "target_results": {self.cid: {"state": "completed_no_results"}},
            "query_plans": {self.cid: {"profile_hash": "e" * 64}},
            "superseded_targets": {self.cid: {"task_id": "selected-replacement"}},
            "request": {"department": "baby"}, "request_hash": "1" * 64,
            "source": "flipkart", "route": "amazon-search", "run_id": "another-run",
            "receipt_refs": ["new-physical-receipt"], "attempt_refs": ["new-physical-attempt"],
            "status": "blocked", "tries": 2, "reconcile_required": True,
            "next_retry_at": "2026-10-07T10:00:00+00:00", "retryable": True,
            "unknown_future_transport_field": {"meaning": "must remain exact"},
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                task = dict(deepcopy(self.task), **{field: value})
                self.assertNotEqual(before, functional_task_projection(task, self.cid))

    def test_malformed_task_projection_refuses(self):
        for task in (None, {}, dict(self.task, targets=[]), dict(self.task, query_plans=None),
                     dict(self.task, revision=True), dict(self.task, revision="2")):
            with self.subTest(task_type=type(task).__name__):
                with self.assertRaises(SafetyError):
                    functional_task_projection(task, self.cid)

    def test_functional_scope_ignores_foreign_changes_but_legacy_scope_does_not(self):
        functional = selection_scope(self.state, self.cid, source_route_contract=FUNCTIONAL_CONTRACT)
        legacy = selection_scope(self.state, self.cid, source_route_contract=CONTRACT)
        self.add_foreign_maps()
        self.assertEqual(functional, selection_scope(self.state, self.cid, source_route_contract=FUNCTIONAL_CONTRACT))
        self.assertNotEqual(legacy, selection_scope(self.state, self.cid, source_route_contract=CONTRACT))

    def test_foreign_relevance_is_ignored_only_by_functional_dependencies(self):
        self.add_foreign()
        before = route_dependencies(self.state, self.cid, functional=True)
        self.state["source_checks"]["foreign-review"] = self.foreign_relevance()
        self.assertEqual(before, route_dependencies(self.state, self.cid, functional=True))
        self.assertIn("foreign-review", route_dependencies(self.state, self.cid)[0]["source_checks"])
        self.assertEqual(self.product["source_scope_hash"], digest(selection_scope(self.state, self.cid,
            source_route_contract=FUNCTIONAL_CONTRACT)))

    def test_selected_or_mixed_relevance_and_physical_checks_remain_dependencies(self):
        self.add_foreign()
        row = self.foreign_relevance()
        row["results"].append({"concept_id": self.cid, "description_hash": self.task["targets"][self.cid],
            "state": "insufficient", "evidence_refs": self.task["receipt_refs"], "reason": "Selected interpretation."})
        self.state["source_checks"]["selected-mixed-review"] = row
        self.assertIn("selected-mixed-review", route_dependencies(self.state, self.cid, functional=True)[0]["source_checks"])
        self.assertNotEqual(self.product["source_scope_hash"], digest(selection_scope(self.state, self.cid,
            source_route_contract=FUNCTIONAL_CONTRACT)))
        del self.state["source_checks"]["selected-mixed-review"]
        physical = dict(row, kind=CHECK_KIND, results=[])
        self.state["source_checks"]["physical-check"] = physical
        self.assertIn("physical-check", route_dependencies(self.state, self.cid, functional=True)[0]["source_checks"])

    def test_malformed_relevance_cannot_be_silently_filtered(self):
        self.state["source_checks"]["bad-relevance"] = {"kind": RELEVANCE_KIND, "task_id": self.task_id, "results": {}}
        with self.assertRaisesRegex(SafetyError, "SOURCE_RELEVANCE_INVALID"):
            route_dependencies(self.state, self.cid, functional=True)

    def test_raw_frozen_hashes_stay_original_while_functional_currentness_is_scoped(self):
        original = read_original_json(self.product["functional_route_proof"]["records"]["query_tasks"][self.task_id])
        expected = self.product["source_revisions"]["query_tasks"][self.task_id]
        self.assertEqual(expected, digest(original))
        self.add_foreign_maps()
        self.assertNotEqual(expected, digest(self.task))
        self.assertTrue(source_revisions_current(self.state, self.product))
        legacy = dict(self.product, recommendation_contract=None)
        self.assertFalse(source_revisions_current(self.state, legacy))

    def test_selected_task_changes_still_make_original_revisions_stale(self):
        for field, value in (("targets", {self.cid: "f" * 64}),
                             ("target_results", {self.cid: {"state": "completed_no_results"}}),
                             ("query_plans", {self.cid: {"profile_hash": "e" * 64}}),
                             ("superseded_targets", {self.cid: {"task_id": "selected-replacement"}}),
                             ("request", {"department": "baby"}), ("receipt_refs", []),
                             ("status", "failed"), ("reconcile_required", True),
                             ("unknown_future_transport_field", "changed")):
            with self.subTest(field=field):
                state = deepcopy(self.baseline)
                state["query_tasks"][self.task_id][field] = value
                self.assertFalse(source_revisions_current(state, self.product))

    def test_missing_task_or_wrong_kind_refuses_scoped_fallback(self):
        for mode in ("missing", "wrong_kind", "malformed"):
            with self.subTest(mode=mode):
                state = deepcopy(self.baseline)
                if mode == "missing": del state["query_tasks"][self.task_id]
                elif mode == "wrong_kind": state["query_tasks"][self.task_id]["kind"] = "different_kind"
                else: state["query_tasks"][self.task_id] = None
                self.assertFalse(source_revisions_current(state, self.product))

    def test_other_original_record_changes_are_never_ignored(self):
        self.add_foreign_maps()
        rid = next(iter(self.product["source_revisions"]["observations"]))
        self.state["observations"][rid]["unexpected_changed_fact"] = True
        self.assertFalse(source_revisions_current(self.state, self.product))

    def test_missing_or_bad_frozen_owner_refuses_even_if_projection_would_match(self):
        self.add_foreign_maps()
        for mode in ("missing_proof", "wrong_concept", "wrong_meaning", "wrong_date", "bad_proof_hash",
                     "missing_original", "bad_original_hash", "expected_digest_changed"):
            with self.subTest(mode=mode):
                product = deepcopy(self.product)
                proof = product["functional_route_proof"]
                if mode == "missing_proof": del product["functional_route_proof"]
                elif mode == "wrong_concept": proof["concept_id"] = self.foreign
                elif mode == "wrong_meaning": proof["meaning_hash"] = "f" * 64
                elif mode == "wrong_date": proof["as_of"] = "2026-10-07T10:00:00+00:00"
                elif mode == "bad_proof_hash": proof["proof_hash"] = "f" * 64
                elif mode == "missing_original": del proof["records"]["query_tasks"][self.task_id]
                elif mode == "bad_original_hash": proof["records"]["query_tasks"][self.task_id]["sha256"] = "f" * 64
                else: product["source_revisions"]["query_tasks"][self.task_id] = "f" * 64
                self.assertFalse(source_revisions_current(self.state, product))

    def test_actual_selection_gate_keeps_packet_after_foreign_target_maps_and_relevance(self):
        self.add_foreign_maps()
        self.state["source_checks"]["foreign-review"] = self.foreign_relevance()
        self.assertIsNone(_current_report_selection(self.issue, self.eligible(), self.state, self.record))

    def test_actual_selection_gate_rejects_selected_source_change(self):
        self.task["target_results"][self.cid]["reason"] = "Changed selected interpretation."
        self.task["revision"] += 1
        self.assertEqual(_current_report_selection(self.issue, self.eligible(), self.state, self.record),
                         "REPORT_SELECTED_SOURCE_CHANGED")

    def test_actual_reservation_accepts_foreign_target_changes_with_original_frozen_bytes(self):
        self.add_foreign_maps()
        memory = self.clone_memory()
        hold = self.reserve(memory)
        self.assertEqual(hold["selection_hash"], self.issue["selection_hash"])
        self.assertEqual(hold["state"], "reserved")
        self.assertEqual((self.root / "data/outputs/functional-test-issue/revision-1/package-input.json").read_bytes(),
                         self.original_input)
        self.assertEqual(memory.snapshot()["published_history"], {})

    def test_actual_foreign_relevance_producer_does_not_invalidate_selection_or_reservation(self):
        self.add_foreign()
        memory = self.clone_memory()
        review = bind_source_relevance(memory, self.task_id, self.foreign_relevance()["results"], as_of=NOW)
        self.state = memory.snapshot()
        self.assertNotIn(review, route_dependencies(self.state, self.cid, functional=True)[0]["source_checks"])
        self.assertIsNone(_current_report_selection(self.issue, self.eligible(), self.state, self.record))
        self.assertEqual(self.reserve(memory)["state"], "reserved")

    def test_actual_reservation_rejects_relevant_task_changes_without_creating_hold(self):
        self.task["request"] = {"department": "baby"}
        memory = self.clone_memory()
        with self.assertRaisesRegex(SafetyError, "ISSUE_SOURCE_FACTS_CHANGED"):
            self.reserve(memory)
        self.assertEqual(memory.snapshot()["issue_reservations"], {})

    def test_frozen_route_reader_still_validates_raw_original_capsules(self):
        before = deepcopy(self.product["functional_route_proof"])
        self.add_foreign_maps()
        self.assertTrue(source_revisions_current(self.state, self.product))
        displayed = next(row for row in self.dataset["products"] if row["concept_id"] == self.cid)
        validate_functional_route_proof(displayed)
        self.assertEqual(displayed["functional_route_proof"], before)
        self.assertEqual(self.product["functional_route_proof"], before)
        self.assertEqual(self.product["source_revisions"]["query_tasks"][self.task_id],
                         before["records"]["query_tasks"][self.task_id]["sha256"])

    def test_actual_foreign_supersession_closure_and_current_js_reader(self):
        memory = self.clone_memory()
        memory.put("concepts", self.foreign, {"description": dict(deepcopy(
            self.state["concepts"][self.cid]["description"]), category="Home and Decor"), "status": "provisional"})
        shared = prepare_source_tasks(memory, [{"route": "amazon-bestsellers", "request": {"department": "home"},
            "concept_ids": [self.cid, self.foreign]}], run_id="target-closure", as_of=NOW)["task_ids"][0]
        replacement = prepare_source_tasks(memory, [{"route": "amazon-bestsellers",
            "request": {"department": "kitchen/1380374031"}, "concept_ids": [self.foreign],
            "supersedes_task_ids": [shared]}], run_id="target-closure", as_of=NOW)["task_ids"][-1]
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        packet_root = Path(temporary.name)
        _, pool, issue, dataset = functional_packet_fixture(packet_root, memory=memory)
        product = next(row for row in issue["products"] if row["concept_id"] == self.cid)
        raw_shared = read_original_json(product["functional_route_proof"]["records"]["query_tasks"][shared])
        self.assertEqual(raw_shared, memory.snapshot()["query_tasks"][shared])
        self.assertEqual(raw_shared["superseded_targets"][self.foreign]["task_id"], replacement)
        self.assertNotIn(replacement, product["functional_route_proof"]["records"]["query_tasks"])
        self.assertNotIn(replacement, product["source_revisions"]["query_tasks"])
        displayed = next(row for row in dataset["products"] if row["concept_id"] == self.cid)
        validate_functional_route_proof(displayed)
        state = memory.snapshot()
        state["query_tasks"][replacement]["status"] = "blocked"
        state["query_tasks"][replacement]["revision"] += 1
        self.assertTrue(source_revisions_current(state, product))
        self.assertEqual(product["source_scope_hash"], digest(selection_scope(state, self.cid,
            source_route_contract=FUNCTIONAL_CONTRACT)))
        eligible = {row["concept_id"]: row for row in pool["eligible"]}
        self.assertIsNone(_current_report_selection(issue, eligible, state, self.record))
        written = generate_report_narratives(memory, issue, dataset, WritingFixture())
        self.assertFalse(written["failures"], written)
        facts = written["dataset"]
        manifest = {"schema_version": 2, "fit_policy_id": "fit-10-v1",
            "criterion_ids": facts["criterion_ids"], "kpi_total": 10,
            "workbook_contract": "fit-checklist-examples-v2", "recommendation_contract": RECOMMENDATION_CONTRACT,
            "opportunity_policy_id": facts["opportunity_policy_id"], "languages": ["en", "hi-Latn"],
            "asset_receipt_hash": digest(facts["report_asset_receipt"])}
        # The current exported reader is used directly, not its broader portal
        # suite or any hosted/release command. It sees raw producer bytes only.
        command = """
import {readFileSync} from 'node:fs';
import {webcrypto} from 'node:crypto';
import {validateFunctionalPacket} from './portal/release-admin.mjs';
globalThis.crypto ??= webcrypto;
const {facts,manifest,asOf}=JSON.parse(readFileSync(0,'utf8'));
await validateFunctionalPacket(facts,manifest,{asOf});
process.stdout.write('current_reader_accepted\\n');
"""
        result = subprocess.run(["node", "--input-type=module", "-e", command],
            input=json.dumps({"facts": facts, "manifest": manifest, "asOf": NOW}), text=True,
            capture_output=True, cwd=Path(__file__).resolve().parents[1], timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "current_reader_accepted\n")

    def test_selected_supersession_retains_replacement_and_physical_receipts(self):
        replacement = "selected-replacement"
        physical = "selected-replacement-receipt"
        self.task["superseded_targets"] = {self.cid: {"task_id": replacement}}
        next_task = deepcopy(self.task)
        next_task.pop("superseded_targets")
        next_task["targets"] = {self.cid: self.task["targets"][self.cid]}
        next_task["receipt_refs"] = [physical]
        self.state["query_tasks"][replacement] = next_task
        self.state["source_checks"][physical] = {"kind": CHECK_KIND, "task_id": replacement,
            "state": "failed", "at": NOW, "envelope": {"capture_ids": [], "observation_ids": [], "evidence_refs": []}}
        selected, _ = _functional_route_state(self.state, self.cid)
        self.assertIn(replacement, selected["query_tasks"])
        self.assertIn(physical, selected["source_checks"])
        self.assertEqual(selected["query_tasks"][self.task_id], self.task)

    def test_writing_reuses_original_rows_after_real_foreign_target_preparation(self):
        runner = WritingFixture()
        first = generate_report_narratives(self.memory, self.issue, self.dataset, runner)
        self.assertFalse(first["failures"], first)
        calls = runner.calls
        self.memory.put("concepts", self.foreign, {"description": deepcopy(self.state["concepts"][self.cid]["description"]),
                                                   "status": "provisional"})
        prepare_source_tasks(self.memory, [{"route": "amazon-bestsellers", "request": {"department": "kitchen"},
            "concept_ids": [self.foreign]}], run_id="target-currentness", as_of=NOW)
        _, _, issue, dataset = functional_packet_fixture(self.root, memory=self.memory, revision=2)
        self.assertEqual(make_narrative_input(self.dataset, "en"), make_narrative_input(dataset, "en"))
        result = generate_report_narratives(self.memory, issue, dataset, runner)
        self.assertFalse(result["failures"], result)
        self.assertEqual(runner.calls, calls)
        self.assertEqual(first["writing_item_refs"], result["writing_item_refs"])
        self.assertEqual((self.root / "data/outputs/functional-test-issue/revision-1/package-input.json").read_bytes(),
                         self.original_input)


if __name__ == "__main__":
    unittest.main()
