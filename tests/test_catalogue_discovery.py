"""Finite actual-capture admission fixtures; no browser/provider/model calls."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.catalogue_discovery import (VERSION, advance_discovery_cycle,
    create_catalogue_discovery_step, prepare_discovery_cycle)
from product_radar.catalogue import load_catalogue
from product_radar.models import SafetyError, digest
from product_radar.marketplace_browser import validate_request
from product_radar.research_memory import ResearchMemory, key
from product_radar.storage import Journal, atomic_json, load_json
from test_catalogue_expansion import fixture
from test_research_flow import FakeRunner

NOW = "2026-10-08T10:00:00+00:00"


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.catalogue = fixture(); self.calls = []; self.replays = []; self.admissions = []

    def handler(self, *, state="usable", throws=None, recovered=False, title="Blue sink caddy", actual=True):
        def collect(context):
            (self.replays if recovered else self.calls).append(deepcopy({k: v for k, v in context.items() if k != "record_dispatch"}))
            if throws:
                raise throws
            aid = context["acquisition_id"]; folder = self.memory.root / "native-fixture" / aid
            capture_id = key("capture", ["amazon", aid]); oid = key("observation", aid)
            reference = folder / "original.json"
            raw = {"route": context["route"], "acquisition_id": aid, "state": state}
            atomic_json(reference, raw)
            capture = {"source": "amazon", "acquisition_id": aid, "observed_at": NOW,
                "reference": {"route": context["route"], "request": context["request"]},
                "capture_hash": digest(raw), "mapping_status": state}
            old = self.memory.snapshot()["captures"].get(capture_id)
            self.memory.put("captures", capture_id, capture, previous=old)
            observations = []
            if state in {"usable", "partial"}:
                semantic = {"title": title, "product_url": "https://www.amazon.in/dp/B012345678"}
                row = {"source": "amazon", "acquisition_id": aid, "capture_id": capture_id,
                       "observed_at": NOW, "semantic_id": key("item", semantic), "semantic": semantic, "facts": {}}
                previous = self.memory.snapshot()["observations"].get(oid)
                self.memory.put("observations", oid, row, previous=previous); observations = [oid]
            if actual is True and context.get("record_dispatch"):
                context["record_dispatch"]()
            return {"schema_version": 1, "route": context["route"], "acquisition_id": aid, "observed_at": NOW,
                    "state": state, "dispatch_started": actual, "reference": str(reference),
                    "capture_ids": [capture_id], "observation_ids": observations, "target_results": [],
                    "reason": "Synthetic actual-capture route fixture."}
        return {"handler": collect, "reconcile": collect}

    def no_admission(self, memory, runner, **options):
        self.admissions.append(deepcopy(options))

    def prepare(self, **options):
        return prepare_discovery_cycle(self.memory, "programme-one", self.catalogue,
            as_of=NOW, handlers=options.pop("handlers", {"meta-ads": self.handler()}),
            include_lists=options.pop("include_lists", False), **options)

    def advance(self, record, handlers, **options):
        return advance_discovery_cycle(self.memory, record["id"], handlers=handlers, as_of=NOW,
            admission=options.pop("admission", self.no_admission), **options)

    def test_preparation_freezes_once_without_calls_products_paid_attempts_or_keyword_padding(self):
        record = self.prepare(); before = self.memory.snapshot()
        self.assertEqual(record["kind"], VERSION)
        self.assertEqual(record["planned_actions"][0]["phrase"], "sink caddy")
        self.assertEqual(self.calls, [])
        self.assertEqual(before["concepts"], {})
        self.assertFalse(self.memory.journal.path.exists())
        changed = deepcopy(self.catalogue); changed["revision"] = "later"
        again = prepare_discovery_cycle(self.memory, "programme-one", changed,
            handlers={"amazon-search": self.handler()}, as_of="2026-10-09T10:00:00+00:00")
        self.assertEqual(again, record)
        self.assertEqual(before, self.memory.snapshot())

    def test_failed_meta_falls_back_to_same_compact_type_without_bestseller_claim(self):
        handlers = {"meta-ads": self.handler(state="blocked"), "amazon-search": self.handler()}
        record = self.prepare(handlers=handlers)
        result = self.advance(record, handlers)
        self.assertEqual([r["route"] for r in self.calls], ["meta-ads", "amazon-search"])
        self.assertEqual([r["request"].get("query") or r["request"].get("term") for r in self.calls], ["sink caddy", "sink caddy"])
        self.assertTrue(all(r["concept_ids"] == [] for r in self.calls))
        self.assertEqual(result["actual_dispatches"], 2)
        self.assertEqual(result["new_concept_ids"], [])
        self.assertEqual(result["description_pending"], 1)
        self.assertEqual(self.admissions[0]["acquisition_ids"], [self.calls[1]["acquisition_id"]])
        self.assertEqual(result["paid_actor_calls"], 0)

    def test_captured_cards_are_not_new_products_before_existing_matching(self):
        handlers = {"amazon-search": self.handler()}; record = self.prepare(handlers=handlers)
        result = self.advance(record, handlers)
        self.assertEqual(result["captured_cards"], 1)
        self.assertEqual(result["unique_source_items"], 1)
        self.assertEqual(result["new_concept_ids"], [])
        self.assertEqual(result["pending_actions"], 1)
        self.assertEqual(result["state"], "work_remaining")

    def test_replay_of_pending_product_interpretation_makes_no_second_external_request(self):
        handlers = {"amazon-search": self.handler()}; record = self.prepare(handlers=handlers)
        first = self.advance(record, handlers)
        second = self.advance(record, handlers)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(second["external_handler_calls_this_round"], 0)
        self.assertEqual(first["description_pending"], second["description_pending"])

    def test_actual_existing_a_b_flow_admits_identity_and_retains_frozen_owner_scope(self):
        handlers = {"amazon-search": self.handler()}; record = self.prepare(handlers=handlers)
        runner = FakeRunner()
        result = self.advance(record, handlers, runner=runner, admission=None)
        self.assertEqual(len(result["new_concept_ids"]), 1)
        self.assertEqual(result["description_pending"], 0)
        self.assertEqual(result["identity_pending"], 0)
        self.assertEqual(result["state"], "checked")
        self.assertFalse(result["original_product_pool_changed"])
        self.assertEqual(result["new_products_need_full_source_review"], result["new_concept_ids"])
        self.assertEqual(record["baseline_concepts"], [])
        original_calls = len(runner.calls)
        second = self.advance(record, handlers, runner=runner, admission=None)
        self.assertEqual(second["new_concept_ids"], result["new_concept_ids"])
        self.assertEqual(len(runner.calls), original_calls)
        self.assertEqual(len(self.calls), 1)

    def test_same_family_new_seller_or_colour_is_known_after_existing_b_match(self):
        runner = FakeRunner(); first_handlers = {"amazon-search": self.handler(title="Blue sink caddy")}
        first = self.prepare(handlers=first_handlers)
        initial = self.advance(first, first_handlers, runner=runner, admission=None)
        cid = initial["new_concept_ids"][0]
        later = "2026-10-16T10:00:00+00:00"
        second_handlers = {"flipkart-search": self.handler(title="Pink sink caddy")}
        second = prepare_discovery_cycle(self.memory, "programme-two", self.catalogue,
            handlers=second_handlers, include_lists=False, as_of=later)
        result = advance_discovery_cycle(self.memory, second["id"], handlers=second_handlers,
            runner=runner, as_of=later)
        self.assertEqual(result["new_concept_ids"], [])
        self.assertEqual(result["known_concept_ids"], [cid])
        self.assertEqual(len(self.memory.snapshot()["concepts"]), 1)

    def test_navigation_uncertainty_reconciles_original_scope_not_new_handler(self):
        original = {"amazon-search": self.handler(throws=TimeoutError("fixture"))}
        record = self.prepare(handlers=original)
        first = self.advance(record, original)
        self.assertEqual(first["actual_dispatches"], 0)
        action = next(iter(self.memory.snapshot()["batch_runs"][record["id"]]["actions"].values()))
        self.assertIsNone(action["dispatch_started"])
        recovered = self.handler(recovered=True)
        result = self.advance(record, {"amazon-search": recovered})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.replays), 1)
        self.assertEqual(self.replays[0]["acquisition_id"], self.calls[0]["acquisition_id"])
        self.assertTrue(self.replays[0]["reconciling"])
        self.assertEqual(result["actual_dispatches"], 1)

    def test_saved_envelope_can_be_applied_after_local_state_commit_conflict_without_recollecting(self):
        handlers = {"amazon-search": self.handler()}; record = self.prepare(handlers=handlers)
        self.advance(record, handlers)
        saved = self.memory.snapshot()["batch_runs"][record["id"]]
        changed = deepcopy(saved)
        for row in changed["actions"].values():
            row["state"] = "execution_uncertain"
        self.memory.put("batch_runs", record["id"], changed, previous=saved)
        result = self.advance(record, handlers)
        self.assertEqual(result["external_handler_calls_this_round"], 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result["captured_cards"], 1)

    def test_unreadable_original_reference_never_invents_success_or_admission(self):
        base = self.handler(); actual = base["handler"]
        def broken(context):
            result = actual(context); Path(result["reference"]).unlink(); return result
        handlers = {"amazon-search": {"handler": broken, "reconcile": broken}}
        record = self.prepare(handlers=handlers)
        result = self.advance(record, handlers)
        self.assertEqual(result["failure_count_this_round"], 1)
        self.assertEqual(self.admissions, [])
        self.assertEqual(result["new_concept_ids"], [])

    def test_public_list_and_family_lanes_are_bounded_separate_choices(self):
        handlers = {"amazon-bestsellers": self.handler(), "amazon-movers-and-shakers": self.handler(),
                    "amazon-search": self.handler()}
        record = self.prepare(handlers=handlers, include_lists=True, action_limit=4)
        self.assertEqual(len(record["planned_actions"]), 4)
        self.assertEqual(record["planned_actions"][0]["kind"], "department_list")
        self.assertEqual(record["planned_actions"][1]["kind"], "family_query")
        self.assertTrue(all("query" not in row["questions"][0]["request"] for row in record["planned_actions"] if row["kind"] == "department_list"))
        result = self.advance(record, handlers, max_actions=2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result["planned_actions"], 4)
        self.assertGreater(result["pending_actions"], 0)

    def test_list_departments_match_installed_product_source_builder(self):
        from product_radar.source_coverage import product_source_request_builder
        handlers = {"amazon-bestsellers": self.handler(), "amazon-movers-and-shakers": self.handler()}
        record = prepare_discovery_cycle(self.memory, "installed-departments", load_catalogue(),
            handlers=handlers, include_lists=True, action_limit=8, as_of=NOW)
        lists = [row for row in record["planned_actions"] if row["kind"] == "department_list"]
        self.assertEqual(len(lists), 8)
        builder = product_source_request_builder()
        state = self.memory.snapshot()
        actual = {}
        for row in lists:
            question = row["questions"][0]
            expected = builder({"concept_id": "unused-list-id", "description": {"category": row["category"]},
                "description_hash": "unused-list-hash", "state": state, "route": question["route"]})
            self.assertEqual(question["request"], expected)
            validate_request(question["request"])
            actual[row["category"]] = expected["department"]
        self.assertEqual(actual, {"Home and Decor": "kitchen/1380374031", "Home and Kitchen": "kitchen",
            "Baby Products": "baby", "Travel and Outdoor": "luggage"})

    def test_new_family_searches_use_complete_installed_contract_and_original_sparse_cycles_stay_frozen(self):
        handlers = {"amazon-search": self.handler(), "flipkart-search": self.handler()}
        record = self.prepare(handlers=handlers)
        for question in record["planned_actions"][0]["questions"]:
            validate_request(question["request"])
            self.assertEqual(question["request"]["term"], "sink caddy")
        previous = self.memory.snapshot()["batch_runs"][record["id"]]
        legacy = deepcopy(previous)
        for question in legacy["planned_actions"][0]["questions"]:
            question["request"] = {"query": "sink caddy", "country": "IN", "limit": 30}
        self.memory.put("batch_runs", record["id"], legacy, previous=previous)
        again = self.prepare(handlers=handlers)
        self.assertEqual(again, legacy)

    def test_real_installed_marketplace_adapter_capture_import_and_identity_flow_work_together(self):
        from unittest.mock import patch
        from product_radar.marketplace_browser import capture_marketplace
        from product_radar.source_coverage import create_marketplace_handlers
        from test_marketplace_browser import snapshot, transport
        actual_requests = []
        def capture(req, acquisition_id, **options):
            validate_request(req); actual_requests.append(deepcopy(req))
            with patch("product_radar.marketplace_browser.utc_now", return_value=NOW):
                return capture_marketplace(req, acquisition_id, **options,
                    snapshot_reader=lambda request: transport(snapshot(request)))
        handlers = create_marketplace_handlers(self.memory, data_root=self.root / "installed-captures", capture=capture)
        record = self.prepare(handlers=handlers)
        result = self.advance(record, handlers, runner=FakeRunner(), admission=None)
        self.assertEqual(len(actual_requests), 1)
        self.assertEqual(result["description_pending"], 0)
        self.assertEqual(len(result["new_concept_ids"]), 1)
        before = self.memory.snapshot()
        second = self.advance(record, handlers, runner=FakeRunner(), admission=None)
        self.assertEqual(len(actual_requests), 1)
        self.assertEqual(second["external_handler_calls_this_round"], 0)
        self.assertEqual(before, self.memory.snapshot())
        self.assertEqual(result["paid_actor_calls"], 0)

    def test_callback_has_exact_n22_shape_and_does_not_change_frozen_product_ids(self):
        handlers = {"amazon-search": self.handler()}
        callback = create_catalogue_discovery_step(self.catalogue, include_lists=False, admission=self.no_admission)
        owner_record = {"programme_id": "owner-one", "concept_ids": ["existing"], "source_run_id": "source156"}
        original = deepcopy(owner_record)
        result = callback(self.memory, owner_record, {"at": NOW, "handlers": handlers, "runner": None, "fit_limit": 10})
        self.assertIsInstance(result["state"], str)
        self.assertEqual(owner_record, original)
        self.assertEqual(len(self.calls), 1)

    def test_recent_exact_query_is_not_reissued_as_unused_research_in_next_cycle(self):
        handlers = {"amazon-search": self.handler()}; record = self.prepare(handlers=handlers)
        self.advance(record, handlers)
        result = prepare_discovery_cycle(self.memory, "another-programme", self.catalogue,
            handlers=handlers, include_lists=False, as_of="2026-10-09T10:00:00+00:00")
        self.assertEqual(result["planned_actions"], [])
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
