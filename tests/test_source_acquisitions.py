"""Synthetic saved ownership/proof only; no source, browser, model or process."""

from copy import deepcopy
from hashlib import sha256
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory, import_capture, key
from product_radar.research_questions import physical_request_fingerprint
from product_radar.source_acquisitions import (
    ACQUISITION_KIND, CAPTURE_SOURCES, claim_source_acquisition, mark_source_dispatching,
    mark_source_execution_unknown, record_source_parse, record_source_response, recover_source_prelaunch,
)
from product_radar.storage import Journal, atomic_json

NOW = "2026-10-09T10:00:00+00:00"
AFTER = "2026-10-09T10:01:00+00:00"
DUE = "2026-10-16T10:00:00+00:00"
LATER = "2026-10-17T10:00:00+00:00"


class SourceAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "captured", Journal(self.root / "finance"), evidence_type="synthetic")
        self.route = "amazon-search"
        self.request = {"query": "draining sink caddy", "country": "IN", "limit": 30}

    def claim(self, **kwargs):
        return claim_source_acquisition(self.memory, kwargs.pop("route", self.route), kwargs.pop("request", self.request),
            owner_id=kwargs.pop("owner_id", "question-one"), as_of=kwargs.pop("as_of", NOW), **kwargs)

    def row(self, acquisition):
        return self.memory.snapshot()["batch_runs"][acquisition["acquisition_id"]]

    def owners(self):
        return {k: v for k, v in self.memory.snapshot()["batch_runs"].items() if v.get("kind") == ACQUISITION_KIND}

    def save_receipt(self, acquisition, *, state="usable", dispatched=True, coverage=True,
                     observed_at=NOW, folder=None, **changes):
        folder = folder or self.memory.root / acquisition["acquisition_id"]
        snapshot = folder / "snapshot.json"
        atomic_json(snapshot, {"title": "Synthetic source", "cards": [{"title": "Sink caddy"}]})
        receipt = {"route": self.route, "request": deepcopy(self.request),
            "acquisition_id": acquisition["acquisition_id"], "attempt_id": acquisition["original_attempt_id"],
            "observed_at": observed_at, "dispatch_started": dispatched, "state": state,
            "artifacts": {"snapshot.json": sha256(snapshot.read_bytes()).hexdigest()}}
        if coverage:
            receipt["coverage"] = deepcopy(coverage) if isinstance(coverage, dict) else {
                "body_complete": True, "byte_truncated": False, "scope_complete": True,
                "recognized": True, "delivered_count": 30, "intentional_limit": True}
        receipt.update(changes)
        path = folder / "receipt.json"
        atomic_json(path, receipt)
        return path

    def response(self, acquisition, **kwargs):
        return record_source_response(self.memory, acquisition["acquisition_id"],
            receipt_path=self.save_receipt(acquisition, **kwargs), as_of=AFTER)

    def parse(self, acquisition, *, state="usable", parser="source-parser-v1", file="parse-v1.json", **changes):
        row = self.row(acquisition)
        response = self.memory.snapshot()["source_checks"][row["active_response_id"]]
        parsed = {"contract": "saved-source-parse-v1", "source_response_hash": response["response_hash"],
            "parser_contract": parser, "state": state, "records": [] if state in {"empty", "failed"} else [{"title": "Sink caddy"}]}
        parsed.update(changes)
        path = self.memory.root / acquisition["acquisition_id"] / file
        atomic_json(path, parsed)
        return record_source_parse(self.memory, acquisition["acquisition_id"], parsed_path=path,
                                   parser_contract=parser, as_of=AFTER)

    def overwrite(self, table, record_id, value):
        old = self.memory.snapshot()[table][record_id]
        self.memory.put(table, record_id, value, previous=old, event_id=key("fixture-edit", [table, record_id, old, value]))

    def test_global_original_is_shared_across_owners_and_restarts(self):
        first = self.claim(original_acquisition_id="actual-original", original_attempt_id="actual-attempt")
        second = self.claim(owner_id="other-week-and-consumer", original_acquisition_id="do-not-use-this")
        self.assertEqual(first["acquisition_id"], second["acquisition_id"])
        self.assertEqual(second["owner_id"], "question-one")
        self.assertEqual(second["original_attempt_id"], "actual-attempt")
        self.assertEqual(second["action"], "join")
        fresh_memory = ResearchMemory(self.memory.root, self.memory.journal, evidence_type="synthetic")
        again = claim_source_acquisition(fresh_memory, self.route, self.request, owner_id="third", as_of=LATER)
        self.assertEqual(again["acquisition_id"], "actual-original")
        self.assertFalse(again["launch_authority"])
        self.assertEqual(len(self.owners()), 1)
        self.assertFalse(list((self.root / "finance").glob("*.json")))

    def test_complete_physical_scope_and_not_consumer_key_changes_ownership(self):
        first = self.claim()
        equivalent = self.claim(request={**self.request, "schema_version": 1, "route": self.route})
        self.assertEqual(first["acquisition_id"], equivalent["acquisition_id"])
        for changes in ({"limit": 31}, {"query": "wall mounted sink caddy"}, {"country": "US"},
                        {"location": {"lat": 28.6, "lon": 77.2}}):
            other = self.claim(request={**self.request, **changes})
            self.assertNotEqual(first["acquisition_id"], other["acquisition_id"])
        self.assertNotEqual(first["acquisition_id"], self.claim(method="HEAD")["acquisition_id"])
        self.assertNotEqual(first["acquisition_id"], self.claim(capture_options={"images": True})["acquisition_id"])

    def test_damaged_lookup_fingerprint_cannot_hide_retained_legacy_owner(self):
        original = self.claim(original_acquisition_id="retained-legacy-acquisition")
        row = deepcopy(self.memory.snapshot()["batch_runs"][original["acquisition_id"]])
        row["binding"]["physical_request_fingerprint"] = "f" * 64
        self.overwrite("batch_runs", original["acquisition_id"], row)
        with self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_RECORD_INVALID"):
            self.claim(owner_id="other-consumer")
        self.assertEqual(len(self.owners()), 1)

    def saved_closed_route_owner(self, acquisition_id="retired-movers-owner", *, damage=False):
        """A saved owner of the retired Movers & Shakers route, as the live store holds."""
        live = self.claim()
        row = deepcopy(self.memory.snapshot()["batch_runs"][live["acquisition_id"]])
        request = {"node": "kitchen", "url": "https://www.amazon.in/gp/movers-and-shakers/kitchen"}
        row["binding"].update(acquisition_id=acquisition_id, route="amazon-movers-and-shakers", request=request,
            original_attempt_id=acquisition_id, physical_request_fingerprint="f" * 64 if damage else
            physical_request_fingerprint("amazon-movers-and-shakers", request, history=True))
        row["binding_hash"] = digest(row["binding"])
        self.memory.put("batch_runs", acquisition_id, row)
        return live, row

    def test_saved_closed_route_owner_is_verified_history_never_a_shared_read_blocker(self):
        import product_radar.source_acquisitions as module
        live, closed = self.saved_closed_route_owner()
        # Every live-route claim keeps working while the closed owner is in the store.
        self.assertEqual(self.claim(owner_id="other-consumer")["acquisition_id"], live["acquisition_id"])
        other = self.claim(request={**self.request, "query": "wall mounted kitchen organizer"}, owner_id="two")
        self.assertEqual(other["action"], "reserved")
        state = self.memory.snapshot()
        with module.source_readiness_owner_scope(state):
            self.assertEqual(module._owners(state, closed["binding"]["physical_request_fingerprint"]), [])
            self.assertEqual(module._owners(state, physical_request_fingerprint(self.route, self.request))[0][0],
                             live["acquisition_id"])
        self.assertEqual(module._record(state, "retired-movers-owner")["binding"]["route"], "amazon-movers-and-shakers")
        # The closed route is history only: no new fingerprint, claim or question.
        with self.assertRaisesRegex(SafetyError, "RESEARCH_QUESTION_ROUTE_INVALID"):
            physical_request_fingerprint("amazon-movers-and-shakers", closed["binding"]["request"])
        with self.assertRaisesRegex(SafetyError, "RESEARCH_QUESTION_ROUTE_INVALID"):
            self.claim(route="amazon-movers-and-shakers", request=closed["binding"]["request"])
        with self.assertRaisesRegex(SafetyError, "RESEARCH_QUESTION_ROUTE_INVALID"):
            physical_request_fingerprint("not-a-route", closed["binding"]["request"], history=True)

    def test_damaged_closed_route_owner_is_still_refused(self):
        self.saved_closed_route_owner(damage=True)
        with self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_RECORD_INVALID"):
            self.claim(owner_id="other-consumer")

    def test_readiness_scope_validates_all_owners_once_without_changing_lookup_results(self):
        import product_radar.source_acquisitions as module
        first = self.claim()
        other_request = {**self.request, "query": "wall mounted kitchen organizer"}
        second = self.claim(request=other_request, owner_id="question-two")
        state = self.memory.snapshot()
        fingerprints = [physical_request_fingerprint(self.route, value) for value in (self.request, other_request)]
        with patch.object(module, "_record", wraps=module._record) as reads:
            with module.source_readiness_owner_scope(state):
                for _ in range(3):
                    self.assertEqual(module._owners(state, fingerprints[0])[0][0], first["acquisition_id"])
                    self.assertEqual(module._owners(state, fingerprints[1])[0][0], second["acquisition_id"])
            self.assertEqual(reads.call_count, 2)
            module._owners(state, fingerprints[0])
            self.assertEqual(reads.call_count, 4)

    def test_readiness_lookup_cannot_hide_damaged_owner_or_authorize_a_stale_claim(self):
        import product_radar.source_acquisitions as module
        first = self.claim(original_acquisition_id="retained-readiness-owner")
        state = self.memory.snapshot()
        fingerprint = physical_request_fingerprint(self.route, self.request)
        with module.source_readiness_owner_scope(state):
            self.assertEqual(module._owners(state, fingerprint)[0][0], first["acquisition_id"])
            broken = deepcopy(state["batch_runs"][first["acquisition_id"]])
            broken["binding"]["physical_request_fingerprint"] = "f" * 64
            self.overwrite("batch_runs", first["acquisition_id"], broken)
            with self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_RECORD_INVALID"):
                self.claim(owner_id="cannot-borrow-stale-readiness")
        changed = self.memory.snapshot()
        with module.source_readiness_owner_scope(changed):
            with self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_RECORD_INVALID"):
                module._owners(changed, fingerprint)
        self.assertEqual(len(self.owners()), 1)

    def test_readiness_lookup_is_removed_after_an_exception_and_does_not_cross_states(self):
        import product_radar.source_acquisitions as module
        self.claim()
        state = self.memory.snapshot()
        fingerprint = physical_request_fingerprint(self.route, self.request)
        with patch.object(module, "_record", wraps=module._record) as reads:
            with self.assertRaisesRegex(ValueError, "Synthetic readiness interruption"):
                with module.source_readiness_owner_scope(state):
                    module._owners(state, fingerprint)
                    module._owners(deepcopy(state), fingerprint)
                    self.assertEqual(reads.call_count, 2)
                    raise ValueError("Synthetic readiness interruption")
            module._owners(state, fingerprint)
            self.assertEqual(reads.call_count, 3)

    def test_concurrent_claim_rechecks_global_owner(self):
        commit = self.memory.commit
        injected = False
        def competing(event_id, changes, **kwargs):
            nonlocal injected
            if not injected:
                injected = True
                with patch.object(self.memory, "commit", side_effect=commit):
                    self.claim(owner_id="competing-owner")
            return commit(event_id, changes, **kwargs)
        with patch.object(self.memory, "commit", side_effect=competing):
            result = self.claim()
        self.assertEqual(result["action"], "join")
        self.assertEqual(result["owner_id"], "competing-owner")
        self.assertEqual(len(self.owners()), 1)

    def test_response_reuse_keeps_original_date_after_local_metadata_touch(self):
        first = self.claim()
        response = self.response(first)
        row = self.row(first)
        proof = self.memory.snapshot()["source_checks"][response["stage_id"]]
        os.utime(proof["receipt"]["path"], None)
        self.assertEqual(self.claim(owner_id="another", as_of=AFTER)["action"], "reuse")
        self.assertEqual(self.row(first), row)
        self.assertEqual(proof["observed_at"], NOW)

    def test_due_refresh_requires_explicit_due_reason_and_complete_capture(self):
        first = self.claim(); self.response(first)
        self.assertEqual(self.claim(as_of=LATER)["action"], "refresh_due")
        for kwargs in ({"refresh_due_at": NOW, "refresh_reason": "Too early"},
                       {"refresh_due_at": "2026-11-01T10:00:00+00:00", "refresh_reason": "Not due yet"}):
            with self.assertRaises(SafetyError): self.claim(as_of=LATER, **kwargs)
        second = self.claim(owner_id="week-two", as_of=LATER, refresh_due_at=DUE, refresh_reason="Seven-day source refresh.")
        self.assertEqual(second["generation"], 2)
        self.assertNotEqual(second["acquisition_id"], first["acquisition_id"])
        self.assertEqual(self.row(second)["binding"]["previous_acquisition_id"], first["acquisition_id"])
        self.assertEqual(self.claim(as_of=LATER)["action"], "join")
        self.assertEqual(len(self.owners()), 2)

    def test_unknown_dispatch_never_becomes_fresh_origin(self):
        first = self.claim()
        mark_source_dispatching(self.memory, first["acquisition_id"], as_of=NOW)
        mark_source_execution_unknown(self.memory, first["acquisition_id"], reason="Original reply was lost.", as_of=AFTER)
        later = self.claim(owner_id="other-parser-and-programme", as_of=LATER,
                           refresh_due_at=DUE, refresh_reason="A different consumer is waiting.")
        self.assertEqual(later["action"], "join")
        self.assertEqual(later["state"], "execution_unknown")
        self.assertEqual(len(self.owners()), 1)
        self.response(first)
        self.assertEqual(self.claim(as_of=AFTER)["action"], "reuse")

    def test_parser_failure_and_repair_do_not_duplicate_or_refresh_read(self):
        first = self.claim(); self.response(first)
        failure = self.parse(first, state="failed")
        self.assertEqual(failure["state"], "response_saved")
        self.assertEqual(self.row(first)["parse_state"], "failed")
        self.assertEqual(self.claim(as_of=AFTER)["action"], "reuse")
        accepted = self.parse(first, parser="source-parser-v2", file="parse-v2.json")
        self.assertEqual(accepted["state"], "parsed")
        self.assertEqual(len(self.owners()), 1)
        self.assertEqual(len(self.row(first)["parse_ids"]), 2)
        self.assertEqual(len(self.row(first)["response_ids"]), 1)

    def test_parse_must_refer_to_actual_original_response_and_contract(self):
        first = self.claim(); self.response(first)
        with self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_PARSE_INPUT_INVALID"):
            self.parse(first, source_response_hash="a" * 64)
        with self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_PARSE_INPUT_INVALID"):
            self.parse(first, parser_contract="some-other-parser")
        self.assertEqual(self.row(first)["parse_ids"], [])

    def test_complete_recognized_empty_is_not_parser_failure_or_access_block(self):
        first = self.claim(); self.response(first, state="empty")
        self.assertEqual(self.parse(first, state="empty")["state"], "completed_empty")
        self.assertEqual(self.claim(as_of=AFTER)["action"], "reuse")
        other = self.claim(request={**self.request, "query": "other caddy"})
        # The saved request, not the test helper's usual query, owns this result.
        path = self.save_receipt(other, state="blocked", request={**self.request, "query": "other caddy"})
        blocked = record_source_response(self.memory, other["acquisition_id"], receipt_path=path, as_of=AFTER)
        self.assertEqual(blocked["state"], "failed_access")
        self.assertEqual(self.claim(request={**self.request, "query": "other caddy"}, as_of=LATER)["action"], "join")

    def test_missing_or_truncated_completeness_is_never_refreshed_as_complete(self):
        first = self.claim(); self.response(first, coverage=False)
        self.assertEqual(self.claim(as_of=LATER, refresh_due_at=DUE, refresh_reason="Due")["action"], "join")
        self.parse(first, state="empty")
        self.assertEqual(self.row(first)["state"], "parsed")
        self.assertNotEqual(self.row(first)["state"], "completed_empty")
        second_request = {**self.request, "query": "a second caddy"}
        second = self.claim(request=second_request)
        path = self.save_receipt(second, request=second_request, coverage={"body_complete": True, "byte_truncated": True,
            "scope_complete": True, "recognized": True, "delivered_count": 12, "intentional_limit": False})
        record_source_response(self.memory, second["acquisition_id"], receipt_path=path, as_of=AFTER)
        self.assertEqual(self.claim(request=second_request, as_of=LATER)["action"], "join")

    def test_actual_capture_refs_and_dates_are_checked_and_frozen(self):
        first = self.claim(); path = self.save_receipt(first)
        capture = {"source": "amazon", "acquisition_id": first["acquisition_id"], "observed_at": NOW,
            "capture_hash": "b" * 64, "mapping_hash": "c" * 64, "reference": {"request": deepcopy(self.request)}}
        self.memory.put("captures", "original-capture", capture)
        record_source_response(self.memory, first["acquisition_id"], receipt_path=path, as_of=AFTER,
                               capture_ids=["original-capture"])
        altered = self.memory.snapshot()
        altered["captures"]["original-capture"]["observed_at"] = AFTER
        with patch.object(self.memory, "snapshot", return_value=altered), self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_CAPTURE_CHANGED"):
            self.claim(as_of=AFTER)

    def test_wrong_scope_future_date_and_artifact_hash_are_refused(self):
        first = self.claim()
        for changes in ({"request": {**self.request, "country": "US"}}, {"acquisition_id": "other-original"},
                        {"attempt_id": "other-attempt"}, {"observed_at": LATER}, {"dispatch_started": 1},
                        {"artifacts": {"snapshot.json": "a" * 64}}):
            with self.subTest(changes=changes), self.assertRaises(SafetyError):
                self.response(first, **changes)
        self.assertEqual(self.row(first)["response_ids"], [])

    def test_changed_saved_bytes_do_not_authorize_recollection(self):
        first = self.claim(); self.response(first)
        path = self.memory.root / first["acquisition_id"] / "snapshot.json"
        atomic_json(path, {"different": "source bytes"})
        with self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_ARTIFACT_CHANGED"):
            self.claim(owner_id="other", as_of=LATER, refresh_due_at=DUE, refresh_reason="Due")
        self.assertEqual(len(self.owners()), 1)

    def test_idempotent_receipts_preserve_first_recorded_clock(self):
        first = self.claim(); path = self.save_receipt(first)
        original = record_source_response(self.memory, first["acquisition_id"], receipt_path=path, as_of=AFTER)
        again = record_source_response(self.memory, first["acquisition_id"], receipt_path=path, as_of=LATER)
        self.assertTrue(again["reused"])
        self.assertEqual(original["stage_id"], again["stage_id"])
        self.assertEqual(len(self.row(first)["response_ids"]), 1)
        self.assertEqual(self.memory.snapshot()["source_checks"][original["stage_id"]]["recorded_at"], AFTER)

    def test_occupied_or_detached_stage_is_not_trusted_on_replay(self):
        first = self.claim(); path = self.save_receipt(first)
        original = record_source_response(self.memory, first["acquisition_id"], receipt_path=path, as_of=AFTER)
        stage = self.memory.snapshot()["source_checks"][original["stage_id"]]
        self.overwrite("source_checks", original["stage_id"], {**stage, "complete": False})
        with self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_STAGE_INVALID"):
            record_source_response(self.memory, first["acquisition_id"], receipt_path=path, as_of=LATER)

    def test_new_owner_and_dispatch_intent_obey_pause_but_saved_bytes_still_reuse(self):
        first = self.claim(); self.response(first)
        control = {"schema_version": 1, "enabled": False, "at": NOW}
        atomic_json(self.memory.root / "programme_service" / "control.json", {"control": control, "hash": digest(control)})
        self.assertEqual(self.claim(as_of=AFTER)["action"], "reuse")
        with self.assertRaisesRegex(SafetyError, "PROGRAMME_OWNER_PAUSED"):
            self.claim(request={**self.request, "query": "different"})
        self.assertEqual(len(self.owners()), 1)

    def test_source_guard_is_scoped_and_rechecked_before_reservation(self):
        seen = []
        def guard(state, binding):
            seen.append(binding["physical_request_fingerprint"])
            if len(seen) == 2: raise SafetyError("ACTUAL_SCOPED_SOURCE_WAIT")
        with self.assertRaisesRegex(SafetyError, "ACTUAL_SCOPED_SOURCE_WAIT"):
            self.claim(source_guard=guard)
        self.assertEqual(len(self.owners()), 0)
        self.assertEqual(len(set(seen)), 1)
        self.assertEqual(self.claim(request={**self.request, "query": "unrelated"})["action"], "reserved")

    def test_paid_origin_is_label_not_financial_admission_or_free_fallback(self):
        with self.assertRaises(SafetyError): self.claim(collection_channel="funded_provider", incremental_collection_fee="known_zero")
        with self.assertRaises(SafetyError): self.claim(collection_channel="funded_provider", incremental_collection_fee="reserved_paid")
        first = self.claim(collection_channel="funded_provider", incremental_collection_fee="reserved_paid", origin_account_key="account-one")
        other = self.claim(owner_id="new", collection_channel="funded_provider", incremental_collection_fee="reserved_paid", origin_account_key="account-two")
        self.assertEqual(other["acquisition_id"], first["acquisition_id"])
        self.assertEqual(self.row(first)["binding"]["origin_account_key"], "account-one")
        self.assertFalse(first["launch_authority"])
        self.assertFalse(list((self.root / "finance").glob("*.json")))

    def zero_receipt(self, first, *, valid=True):
        folder = self.memory.root / first["acquisition_id"] / "zero"
        row = self.row(first)
        intent_id = row["active_dispatch_intent_id"]
        intent = self.memory.snapshot()["source_checks"][intent_id]
        proof = {"contract": "owned-source-process-exit-v1", "acquisition_id": first["acquisition_id"],
            "attempt_id": first["original_attempt_id"], "physical_request_fingerprint": physical_request_fingerprint(self.route, self.request),
            "owned_process_exited": valid, "request_sent": False, "process_id": 12345,
            "dispatch_intent_id": intent_id, "intent_sequence": intent["intent_sequence"],
            "ownership_token_hash": intent["process_ownership_token_hash"], "started_at": NOW, "exited_at": AFTER}
        atomic_json(folder / "process-exit.json", proof)
        receipt = {"route": self.route, "request": deepcopy(self.request), "acquisition_id": first["acquisition_id"],
            "attempt_id": first["original_attempt_id"], "observed_at": AFTER, "dispatch_started": False,
            "state": "failed", "artifacts": {"process-exit.json": sha256((folder / "process-exit.json").read_bytes()).hexdigest()}}
        atomic_json(folder / "receipt.json", receipt)
        return folder / "receipt.json"

    def test_known_zero_recovery_requires_real_bound_owned_process_exit_artifact(self):
        first = self.claim(incremental_collection_fee="known_zero", collection_channel="direct_local")
        mark_source_dispatching(self.memory, first["acquisition_id"], as_of=NOW)
        mark_source_execution_unknown(self.memory, first["acquisition_id"], reason="Original process needs reconciliation.", as_of=AFTER)
        for path in (self.save_receipt(first, dispatched=False), self.zero_receipt(first, valid=False)):
            with self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_ZERO_PROOF_REQUIRED"):
                recover_source_prelaunch(self.memory, first["acquisition_id"], receipt_path=path, as_of=AFTER)
        path = self.zero_receipt(first)
        recovered = recover_source_prelaunch(self.memory, first["acquisition_id"], receipt_path=path, as_of=AFTER)
        self.assertEqual(recovered["state"], "reserved")
        self.assertFalse(recovered["launch_authority"])
        self.assertEqual(len(self.owners()), 1)
        self.assertEqual(mark_source_dispatching(self.memory, first["acquisition_id"], as_of=AFTER)["state"], "dispatching")
        self.assertEqual(len(self.row(first)["stage_ids"]), 4)

    def test_real_dispatched_response_cannot_be_rewritten_as_never_started(self):
        first = self.claim(); mark_source_dispatching(self.memory, first["acquisition_id"], as_of=NOW); self.response(first)
        with self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_TRANSITION_INVALID"):
            recover_source_prelaunch(self.memory, first["acquisition_id"], receipt_path=self.zero_receipt(first), as_of=AFTER)
        with self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_RECEIPT_CONFLICT"):
            self.response(first, dispatched=None)

    def test_explicit_original_artifact_root_preserves_real_sibling_paths(self):
        sibling = self.root / "direct-marketplace"; sibling.mkdir()
        first = self.claim(artifact_roots=[sibling])
        path = self.save_receipt(first, folder=sibling / "actual-original")
        result = record_source_response(self.memory, first["acquisition_id"], receipt_path=path, as_of=AFTER)
        stage = self.memory.snapshot()["source_checks"][result["stage_id"]]
        self.assertEqual(stage["receipt"]["path"], str(path))
        self.assertEqual(self.claim(as_of=AFTER)["action"], "reuse")
        self.assertFalse((self.memory.root / "actual-original").exists())

    def test_malformed_owner_rows_and_chain_do_not_escape_named_errors(self):
        first = self.claim()
        valid = self.row(first)
        for changes in ({"state": []}, {"response_ids": [{}]}, {"binding": {}}, {"active_response_id": "missing"}):
            broken = {**valid, **changes}
            self.overwrite("batch_runs", first["acquisition_id"], broken)
            with self.subTest(changes=changes), self.assertRaises(SafetyError): self.claim()
            self.overwrite("batch_runs", first["acquisition_id"], valid)
        broken = deepcopy(valid); broken["binding"]["generation"] = 2; broken["binding_hash"] = digest(broken["binding"])
        self.overwrite("batch_runs", first["acquisition_id"], broken)
        with self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_CHAIN_INVALID"): self.claim()

    def test_concurrent_artifact_change_during_commit_is_not_saved(self):
        first = self.claim(); path = self.save_receipt(first)
        commit = self.memory.commit
        def changed(event_id, changes, **kwargs):
            atomic_json(path.parent / "snapshot.json", {"changed": True})
            return commit(event_id, changes, **kwargs)
        with patch.object(self.memory, "commit", side_effect=changed), self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_FILE_CHANGED"):
            record_source_response(self.memory, first["acquisition_id"], receipt_path=path, as_of=AFTER)
        self.assertEqual(self.row(first)["response_ids"], [])

    def test_every_route_maps_to_actual_capture_sources(self):
        from product_radar.source_coverage import ROUTES
        self.assertEqual(set(CAPTURE_SOURCES), set(ROUTES))
        self.assertNotIn("amazon-movers-and-shakers", CAPTURE_SOURCES)  # Retired in Plan R10 W6.
        self.assertLessEqual({"meta-ads": "meta", "amazon-bestsellers": "amazon",
            "amazon-search": "amazon", "flipkart-search": "flipkart",
            "google-trends": "trends", "blinkit": "blinkit", "zepto": "zepto", "instamart": "instamart",
            "google-suppliers": "supplier", "google-maps": "supplier", "indiamart": "supplier",
            "tradeindia": "supplier", "company-pages": "supplier"}.items(), CAPTURE_SOURCES.items())
        # Trends actually uses the existing importer. Supplier routes normally
        # save supplier checks, not captures; this test invents no such rows.
        first = self.claim(route="google-trends")
        path = self.save_receipt(first, route="google-trends")
        imported = import_capture(self.memory, source="trends", acquisition_id=first["acquisition_id"],
            observed_at=NOW, mapped={"status": "empty", "records": [], "rejected": []},
            capture_hash="d" * 64, reference={"file": str(path), "request": deepcopy(self.request)})
        result = record_source_response(self.memory, first["acquisition_id"], receipt_path=path, as_of=AFTER,
                                        capture_ids=[imported["capture_id"]])
        self.assertEqual(result["state"], "response_saved")

    def test_refresh_commit_rechecks_complete_original_bytes(self):
        first = self.claim(); self.response(first)
        commit = self.memory.commit
        def changed(event_id, changes, **kwargs):
            atomic_json(self.memory.root / first["acquisition_id"] / "snapshot.json", {"changed": True})
            return commit(event_id, changes, **kwargs)
        with patch.object(self.memory, "commit", side_effect=changed), self.assertRaisesRegex(SafetyError, "SOURCE_ACQUISITION_FILE_CHANGED"):
            self.claim(as_of=LATER, refresh_due_at=DUE, refresh_reason="The original capture is due.")
        self.assertEqual(len(self.owners()), 1)


if __name__ == "__main__":
    unittest.main()
