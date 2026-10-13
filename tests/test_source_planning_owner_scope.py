"""Disposable planning-only ownership checks; no actual research history.

The fake memory returns fresh copies and applies exact predecessor checks in
RAM. Only unique temporary lock/receipt files are used. No provider, browser,
model, process, installed task or live canonical research store is accessed.
"""

from contextlib import nullcontext
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.models import SafetyError, digest
from product_radar.research_memory import TABLES, change, key
from product_radar.research_questions import physical_request_fingerprint
from product_radar.storage import atomic_bytes
import product_radar.product_queries as queries
import product_radar.source_acquisitions as acquisitions
import product_radar.source_coverage as coverage
import product_radar.supplier_search as suppliers

NOW = "2026-10-12T18:00:00+00:00"
SUPPLIER_ROUTES = ("google-suppliers", "google-maps", "indiamart", "tradeindia")
LOCATION = {"name": "Delhi", "lat": "28.6139", "lon": "77.2090"}


class _Memory:
    """Only disposable in-memory state, not ResearchMemory or its replay."""

    def __init__(self, root, state):
        self.root = root
        self.state = state
        self.commits = []
        self.on_commit = None

    def snapshot(self):
        return deepcopy(self.state)

    def commit(self, event_id, changes, *, validate=None):
        current = self.snapshot()
        if self.on_commit is not None:
            self.on_commit(current)
        if validate is not None:
            validate(current)
        for item in changes:
            old = current[item["table"]].get(item["id"])
            expected = digest(old) if old is not None else None
            if item["previous_hash"] != expected:
                raise SafetyError("RESEARCH_DEPENDENCY_CHANGED")
            current[item["table"]][item["id"]] = deepcopy(item["value"])
        self.state = current
        self.commits.append(event_id)
        return True

    def put(self, table, record_id, value, *, previous=None, event_id=None):
        return self.commit(event_id or key("fixture-put", [table, record_id, value]),
                           [change(table, record_id, value, previous)])


class SourcePlanningOwnerScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        state = {table: {} for table in TABLES}
        self.memory = _Memory(self.root / "synthetic-memory", state)
        for cid, label in (("first", "Sink caddy"), ("second", "Shoe organiser")):
            state["concepts"][cid] = {"status": "active", "description": {
                "label": label, "category": "Home and Kitchen", "function": "Organise household items",
                "mechanism": "manual", "audience": "adult", "features": ["compact"],
                "basis": "observed", "brand": "generic_or_small", "reason": "Synthetic fixture."}}
            item = queries.query_profile_input(state, cid)
            profile = queries._from_type(item, label, method="synthetic")
            ref = key("query-profile", [queries.VERSION, digest(item), profile, "synthetic", None])
            state["source_checks"][ref] = {"kind": "product_query_profile", "version": queries.VERSION,
                "source": "query_profile", "route": "saved_query_planning", "state": "usable", "at": NOW,
                "reference": ref, "concept_id": cid, "description_hash": digest(item["description"]),
                "input_hash": digest(item), "input": deepcopy(item), "profile": profile,
                "method": "synthetic", "job_id": None, "reason": profile["reason"]}
        self.first_owner = self.add_owner("google-suppliers", "Sink caddy", transport="public-http")
        self.second_owner = self.add_owner("indiamart", "Sink caddy", transport="public-browser")
        self.unrelated_owner = self.add_owner("amazon-search", "Unrelated product")
        self.assertEqual(len(self.owner_ids()), 3)

    def owner_ids(self):
        return {rid for rid, row in self.memory.state["batch_runs"].items()
                if row.get("kind") == acquisitions.ACQUISITION_KIND}

    def add_owner(self, route, query, *, transport=None):
        if route in SUPPLIER_ROUTES:
            request = suppliers.prepare_supplier_request({"route": route,
                "request": {"query": query, "country": "IN"}})
            request["transport"] = transport or "public-http"
        else:
            request = {"query": query, "country": "IN"}
        fingerprint = physical_request_fingerprint(route, request)
        aid = key("synthetic-owner", [route, request])
        binding = {"contract": acquisitions.ACQUISITION_KIND, "route": route, "request": request,
            "method": "GET", "capture_options": {}, "physical_request_fingerprint": fingerprint,
            "acquisition_id": aid, "original_attempt_id": aid, "owner_id": "synthetic-question",
            "generation": 1, "previous_acquisition_id": None, "collection_channel": "direct_local",
            "incremental_collection_fee": "known_zero", "origin_account_key": None,
            "artifact_roots": [str(self.root)], "refresh": None}
        self.memory.state["batch_runs"][aid] = {"kind": acquisitions.ACQUISITION_KIND,
            "binding": binding, "binding_hash": digest(binding), "state": "reserved",
            "created_at": NOW, "updated_at": NOW, "stage_ids": [], "response_ids": [],
            "parse_ids": [], "active_response_id": None, "active_parse_id": None,
            "active_dispatch_intent_id": None, "parse_state": "not_started"}
        return aid

    def save_response(self, aid, *, legacy=False):
        owner = self.memory.state["batch_runs"][aid]
        path = self.root / aid / "receipt.json"
        raw = b'{"synthetic":"saved response bytes"}'
        atomic_bytes(path, raw)
        row = {"kind": acquisitions.STAGE_KIND, "stage_type": "response", "acquisition_id": aid,
            "binding_hash": owner["binding_hash"], "receipt": {"path": str(path),
            "sha256": sha256(raw).hexdigest()}, "artifacts": {}, "captures": [],
            "bridge_contract": suppliers.BRIDGE, "dispatch_started": True, "source_state": "insufficient",
            "access": {"http_status": 200, "owned_process_exited": True},
            "parse_reason": "SUPPLIER_ROUTE_LAYOUT_UNRECOGNISED",
            "coverage": {"body_complete": True, "byte_truncated": False}}
        if legacy:
            row["legacy_workflow_id"] = "synthetic-legacy"
        sid = key("source-acquisition-stage", row)
        row["recorded_at"] = NOW
        row["record_hash"] = digest(row)
        self.memory.state["source_checks"][sid] = row
        owner.update(state="response_saved", stage_ids=[sid], response_ids=[sid], active_response_id=sid)
        return path

    def builder(self):
        return coverage.product_source_request_builder(location=LOCATION, run_id="synthetic-week",
                                                       shared_suppliers=True)

    def prepare(self, builder=None):
        return coverage.prepare_all_source_tasks(self.memory, run_id="synthetic-week",
            request_builder=self.builder() if builder is None else builder, concept_ids=["first", "second"], as_of=NOW)

    def context(self, state, cid, route):
        description = state["concepts"][cid]["description"]
        return {"state": state, "concept_id": cid, "route": route, "run_id": "synthetic-week",
                "description": deepcopy(description), "description_hash": digest(description), "as_of": NOW}

    def supplier_walk(self, state, *, scoped):
        result, builder = [], self.builder()
        manager = acquisitions.source_readiness_owner_scope(state) if scoped else nullcontext()
        with manager:
            for cid in ("first", "second"):
                for route in SUPPLIER_ROUTES:
                    context = self.context(state, cid, route)
                    result.append((cid, route, builder(context), context["query_plan"]))
        return result

    def test_builtin_planner_declares_its_pure_planning_scope(self):
        self.assertIs(getattr(self.builder(), "_pure_source_planning", None), True)

    def test_manual_scope_reduces_validation_count_without_changing_requests_or_state(self):
        state = self.memory.snapshot()
        before = digest(state)
        with patch.object(acquisitions, "_record", wraps=acquisitions._record) as validations:
            unscoped = self.supplier_walk(state, scoped=False)
            self.assertEqual(validations.call_count, 48)  # 2 products * 4 routes * 2 lookups * 3 owners.
            validations.reset_mock()
            scoped = self.supplier_walk(state, scoped=True)
            self.assertEqual(validations.call_count, 3)
            self.assertEqual({call.args[1] for call in validations.call_args_list}, self.owner_ids())
        self.assertEqual(scoped, unscoped)
        self.assertEqual(digest(state), before)
        self.assertIsNone(acquisitions._READINESS_OWNERS.get())

    def test_actual_prepare_validates_all_three_owners_once_for_eight_supplier_questions(self):
        with patch.object(acquisitions, "_record", wraps=acquisitions._record) as validations:
            prepared = self.prepare()
            self.assertEqual(validations.call_count, 3)
            self.assertEqual({call.args[1] for call in validations.call_args_list}, self.owner_ids())
        supplier_tasks = [row for row in self.memory.state["query_tasks"].values()
                          if row.get("route") in SUPPLIER_ROUTES]
        self.assertEqual(len(supplier_tasks), 8)
        self.assertEqual(prepared["source_attempts_created"], 0)
        self.assertTrue(all(not row["attempt_refs"] and row["generation"] == 0 for row in supplier_tasks))
        self.assertIsNone(acquisitions._READINESS_OWNERS.get())

    def test_second_preparation_revalidates_fresh_owner_set(self):
        builder = self.builder()
        with patch.object(acquisitions, "_record", wraps=acquisitions._record) as validations:
            self.prepare(builder)
            self.add_owner("amazon-search", "New unrelated product")
            self.prepare(builder)
            self.assertEqual(validations.call_count, 7)  # First snapshot 3; second fresh snapshot 4.
        self.assertIsNone(acquisitions._READINESS_OWNERS.get())

    def test_unrelated_damaged_owner_is_not_hidden_by_requested_fingerprint(self):
        self.memory.state["batch_runs"][self.unrelated_owner]["binding"]["physical_request_fingerprint"] = "f" * 64
        prepared = self.prepare()
        rejected = [row for row in prepared["pending_preparation"] if row["route"] in SUPPLIER_ROUTES]
        self.assertEqual(len(rejected), 8)
        self.assertEqual({row["reason"] for row in rejected}, {"SOURCE_ACQUISITION_RECORD_INVALID"})
        self.assertFalse(any(row["route"] in SUPPLIER_ROUTES for row in self.memory.state["query_tasks"].values()))
        self.assertIsNone(acquisitions._READINESS_OWNERS.get())

    def test_unmarked_mutable_builder_keeps_uncached_lookup_semantics(self):
        seen, injected = [], False
        original = deepcopy(self.memory.state)
        new_id = self.add_owner("amazon-search", "New planner-local owner")
        new_row = self.memory.state["batch_runs"].pop(new_id)
        def builder(context):
            nonlocal injected
            self.assertIsNone(acquisitions._READINESS_OWNERS.get())
            if not injected:
                injected = True
                state = context["state"]
                acquisitions._owners(state, "0" * 64)
                state["batch_runs"][new_id] = deepcopy(new_row)
                rows = acquisitions._owners(state, new_row["binding"]["physical_request_fingerprint"])
                seen.extend(rid for rid, _ in rows)
            return {"query": "Synthetic custom question", "country": "IN"}
        self.prepare(builder)
        self.assertEqual(seen, [new_id])
        self.assertNotIn(new_id, self.memory.state["batch_runs"])
        for rid, owner in original["batch_runs"].items():
            self.assertEqual(self.memory.state["batch_runs"][rid], owner)

    def test_generic_pending_and_not_applicable_results_are_unchanged(self):
        def builder(context):
            self.assertIsNone(acquisitions._READINESS_OWNERS.get())
            if context["route"] == "company-pages":
                raise SafetyError("SOURCE_SUPPLIER_DESTINATIONS_PENDING")
            if context["route"] == "blinkit":
                return {"not_applicable_reason": "Synthetic location outside scope."}
            return {"query": "Synthetic custom question", "country": "IN"}
        prepared = self.prepare(builder)
        self.assertEqual(prepared["physical_questions"], 12)
        self.assertEqual(prepared["product_questions"], 24)
        self.assertEqual(len(prepared["pending_preparation"]), 2)
        self.assertEqual({row["route"] for row in prepared["pending_preparation"]}, {"company-pages"})
        self.assertEqual(prepared["source_attempts_created"], 0)

    def test_truthy_nonboolean_custom_marker_does_not_enable_planning_scope(self):
        for marker in (1, Mock(name="truthy-but-not-an-immutable-planning-contract")):
            with self.subTest(marker_type=type(marker).__name__):
                visits = []
                def builder(context):
                    self.assertIsNone(acquisitions._READINESS_OWNERS.get())
                    visits.append((context["concept_id"], context["route"]))
                    return {"query": "Synthetic custom question", "country": "IN"}
                builder._pure_source_planning = marker
                self.prepare(builder)
                self.assertEqual(len(visits), 28)
                self.assertIsNone(acquisitions._READINESS_OWNERS.get())

    def test_fresh_plan_reads_and_commits_are_outside_advisory_scope(self):
        real_prepare = coverage.prepare_source_tasks
        observations = []
        def fresh_prepare(memory, questions, **options):
            self.assertIsNone(acquisitions._READINESS_OWNERS.get())
            new_id = self.add_owner("amazon-search", "Fresh commit owner")
            fresh = memory.snapshot()
            fingerprint = fresh["batch_runs"][new_id]["binding"]["physical_request_fingerprint"]
            observations.extend(rid for rid, _ in acquisitions._owners(fresh, fingerprint))
            return real_prepare(memory, questions, **options)
        def observe_commit(_state):
            self.assertIsNone(acquisitions._READINESS_OWNERS.get())
        self.memory.on_commit = observe_commit
        with patch.object(coverage, "prepare_source_tasks", side_effect=fresh_prepare):
            self.prepare()
        self.assertEqual(len(observations), 1)
        self.assertTrue(self.memory.commits)

    def test_fresh_claim_refuses_new_damaged_owner_after_advisory_planning(self):
        real_prepare = coverage.prepare_source_tasks
        checks = []
        def fresh_prepare(memory, questions, **options):
            self.assertIsNone(acquisitions._READINESS_OWNERS.get())
            row = memory.state["batch_runs"][self.unrelated_owner]
            original = deepcopy(row)
            row["binding"]["physical_request_fingerprint"] = "f" * 64
            try:
                with self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_RECORD_INVALID"):
                    acquisitions.claim_source_acquisition(memory, "amazon-search", {"query": "Fresh claim", "country": "IN"},
                        owner_id="fresh-synthetic-consumer", as_of=NOW)
                checks.append(True)
            finally:
                memory.state["batch_runs"][self.unrelated_owner] = original
            return real_prepare(memory, questions, **options)
        with patch.object(coverage, "prepare_source_tasks", side_effect=fresh_prepare):
            self.prepare()
        self.assertEqual(checks, [True])
        self.assertEqual(len(self.owner_ids()), 3)

    def test_nested_planning_restores_outer_scope_without_lending_it_to_fresh_prepare(self):
        outer_state = self.memory.snapshot()
        fingerprint = outer_state["batch_runs"][self.unrelated_owner]["binding"]["physical_request_fingerprint"]
        real_prepare = coverage.prepare_source_tasks
        observations = []
        with patch.object(acquisitions, "_record", wraps=acquisitions._record) as validations:
            with acquisitions.source_readiness_owner_scope(outer_state):
                outer_scope = acquisitions._READINESS_OWNERS.get()
                acquisitions._owners(outer_state, fingerprint)
                self.assertEqual(validations.call_count, 3)
                outer_index = outer_scope["index"]
                def fresh_prepare(memory, questions, **options):
                    self.assertIs(acquisitions._READINESS_OWNERS.get(), outer_scope)
                    fresh = memory.snapshot()
                    self.assertIsNot(fresh, outer_state)
                    rows = acquisitions._owners(fresh, fingerprint)
                    observations.extend(rid for rid, _ in rows)
                    return real_prepare(memory, questions, **options)
                with patch.object(coverage, "prepare_source_tasks", side_effect=fresh_prepare):
                    self.prepare()
                self.assertEqual(validations.call_count, 9)  # Outer 3, inner plan 3, fresh prepare 3.
                self.assertIs(acquisitions._READINESS_OWNERS.get(), outer_scope)
                self.assertIs(outer_scope["index"], outer_index)
                acquisitions._owners(outer_state, fingerprint)
                self.assertEqual(validations.call_count, 9)  # The unchanged outer scope was restored intact.
        self.assertEqual(observations, [self.unrelated_owner])
        self.assertIsNone(acquisitions._READINESS_OWNERS.get())

    def assert_exception_cleanup(self, error):
        real_request = suppliers.supplier_request_for_state
        def failing_request(state, route, query):
            real_request(state, route, query)  # Populate the advisory owner index first.
            raise error
        with patch.object(suppliers, "supplier_request_for_state", side_effect=failing_request):
            with self.assertRaises(type(error)) as caught:
                self.prepare()
        self.assertIs(caught.exception, error)
        self.assertIsNone(acquisitions._READINESS_OWNERS.get())
        self.assertFalse(self.memory.commits)
        with patch.object(acquisitions, "_record", wraps=acquisitions._record) as validations:
            acquisitions._owners(self.memory.snapshot(), "0" * 64)
            self.assertEqual(validations.call_count, 3)

    def test_unexpected_builder_error_cleans_up_advisory_scope(self):
        self.assert_exception_cleanup(ValueError("Synthetic builder error"))

    def test_keyboard_interrupt_cleans_up_advisory_scope(self):
        self.assert_exception_cleanup(KeyboardInterrupt("Synthetic owner interruption"))

    def test_system_exit_cleans_up_advisory_scope(self):
        self.assert_exception_cleanup(SystemExit("Synthetic owner exit"))

    def test_builtin_recoverable_gap_does_not_hold_other_supplier_questions(self):
        real_request = suppliers.supplier_request_for_state
        def request(state, route, query):
            if route == "indiamart":
                raise SafetyError("SOURCE_QUESTION_INPUT_PENDING")
            return real_request(state, route, query)
        with patch.object(suppliers, "supplier_request_for_state", side_effect=request):
            prepared = self.prepare()
        rejected = [row for row in prepared["pending_preparation"] if row["route"] == "indiamart"]
        self.assertEqual(len(rejected), 2)
        self.assertEqual({row["reason"] for row in rejected}, {"SOURCE_QUESTION_INPUT_PENDING"})
        self.assertEqual(sum(row["route"] in SUPPLIER_ROUTES for row in self.memory.state["query_tasks"].values()), 6)
        self.assertIsNone(acquisitions._READINESS_OWNERS.get())

    def test_saved_supplier_transport_choices_remain_equal_with_scope(self):
        self.save_response(self.first_owner)
        self.save_response(self.second_owner, legacy=True)
        state = self.memory.snapshot()
        before = digest(state)
        with patch.object(acquisitions, "_file", wraps=acquisitions._file) as files:
            unscoped = self.supplier_walk(state, scoped=False)
            unscoped_reads = files.call_count
            files.reset_mock()
            scoped = self.supplier_walk(state, scoped=True)
            self.assertEqual(files.call_count, unscoped_reads)
            self.assertEqual(files.call_count, 2)
        self.assertEqual(scoped, unscoped)
        transports = {(cid, route): request["transport"] for cid, route, request, _ in scoped}
        self.assertEqual(transports["first", "google-suppliers"], "public-browser")
        self.assertEqual(transports["first", "indiamart"], "public-browser")
        self.assertEqual(transports["second", "google-suppliers"], "public-http")
        self.assertEqual(digest(state), before)

    def test_owner_index_reuse_does_not_cache_original_response_file_proof(self):
        path = self.save_response(self.first_owner)
        state, builder = self.memory.snapshot(), self.builder()
        with acquisitions.source_readiness_owner_scope(state):
            self.assertEqual(builder(self.context(state, "first", "google-suppliers"))["transport"], "public-browser")
            atomic_bytes(path, b'{"synthetic":"changed response bytes"}')
            with self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_ARTIFACT_CHANGED"):
                builder(self.context(state, "first", "google-suppliers"))
        self.assertIsNone(acquisitions._READINESS_OWNERS.get())


if __name__ == "__main__":
    unittest.main()
