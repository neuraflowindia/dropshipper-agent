"""Actual direction capture/admission → later accepted type → bounded scope B."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from product_radar.catalogue_scope_review import (CONTRACT, MODE, PROMPT_VERSION, METHOD,
    catalogue_scope_readiness, review_discovery_catalogue_scopes, validate_scope_payload)
from product_radar.functional_types import accept_functional_definition, effective_functional_definition
from product_radar.models import SafetyError, digest
from product_radar.research_ai import Jobs, b_contract, validate_results
from product_radar.research_memory import key
from product_radar.research_scope import accept_catalogue_scope, current_catalogue_scopes
from product_radar.storage import atomic_json, load_json, safe_path
from product_radar.work_turns import bind_work_turn, close_work_turn, open_work_turn
import test_discovery_waves as waves
from test_functional_queries import DEFINITION
from test_research_flow import FakeRunner


class ScopeRunner:
    identity = "synthetic-catalogue-scope-v1"
    def __init__(self, relationship="narrower", action=None, invalid_second=False):
        self.calls = []; self.relationship = relationship; self.action = action; self.invalid_second = invalid_second
    def __call__(self, role, payload, folder):
        self.calls.append((role, deepcopy(payload)))
        results = [{"id": item["id"], "relationship": self.relationship,
            "references": [item["anchor"]["observation_ref"]["id"]],
            "reason": "The explicit draining mechanism is narrower than the general sink heading."}
            for item in payload["items"]]
        if self.invalid_second and len(results) > 1: results[1]["references"] = ["invented-observation"]
        if self.action: self.action()
        return {"results": results}, {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}


class CatalogueScopeTests(unittest.TestCase):
    def setUp(self):
        # Reuse actual installed direct leaf+parser/importer+wave/A/B admission.
        waves.DiscoveryWaveTests.setUp(self)
        self.mode["amazon-search"] = "usable"
        waves.DiscoveryWaveTests.advance(self, max_actions=3, runner=FakeRunner(), admission=None)
        state = self.memory.snapshot()
        self.cid = next(iter(state["concepts"]))
        self.children = [child for child, row in state["associations"].items() if row.get("concept_id") == self.cid]
        semantics = {state["associations"][child]["semantic_id"] for child in self.children}
        self.refs = sorted(oid for oid, row in state["observations"].items() if row["semantic_id"] in semantics)
        self.accept()

    handlers = waves.DiscoveryWaveTests.handlers
    no_admission = waves.DiscoveryWaveTests.no_admission

    def accept(self, definition=None, *, at=waves.NOW):
        state = self.memory.snapshot()
        return accept_functional_definition(self.memory, self.cid, definition or DEFINITION,
            basis_refs=self.refs, reviewer="synthetic explicit reviewer", reviewed_at=at,
            expected_meaning_hash=effective_functional_definition(state, self.cid)["meaning_hash"],
            example_decisions={child: {"decision": "same", "reason": "Actual observed caddy matches this reviewed type."}
                               for child in self.children})

    def hint(self, **options):
        return catalogue_scope_readiness(self.memory, catalogue=self.catalogue, concept_ids=[self.cid],
            as_of=waves.NOW, runner_available=True, **options)

    def review(self, runner=None, **options):
        return review_discovery_catalogue_scopes(self.memory, runner, catalogue=self.catalogue,
            concept_ids=[self.cid], as_of=waves.NOW, **options)

    def test_real_capture_admission_and_later_definition_bind_without_transferring_votes(self):
        before = self.memory.snapshot()
        hint = self.hint()
        self.assertTrue(hint["model_ready"])
        self.assertEqual(len(hint["pending"]), 1)
        self.assertEqual(before, self.memory.snapshot())
        item = hint["pending"][0]["item"]
        self.assertEqual(item["concept_id"], self.cid)
        self.assertNotIn("report_title", item["definition"])
        self.assertNotIn("price", str(item))
        runner = ScopeRunner(); result = self.review(runner)
        self.assertEqual(result["outcomes"][0]["state"], "current")
        self.assertEqual(len(runner.calls), 1)
        state = self.memory.snapshot(); current = current_catalogue_scopes(state, self.catalogue, self.cid)
        self.assertEqual(current[0]["relationship"], "narrower")
        self.assertEqual(current[0]["method"], METHOD)
        self.assertEqual(current[0]["source_contexts"][0]["observed_at"], waves.NOW)
        for table in before:
            if table not in {"jobs", "source_checks"}: self.assertEqual(before[table], state[table])
        self.assertFalse(self.hint()["ready"])
        self.review(runner)
        self.assertEqual(len(runner.calls), 1)

    def test_unknown_is_a_completed_review_not_a_positive_seeking_loop(self):
        runner = ScopeRunner("unclear")
        self.review(runner)
        again = self.review(runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(again["current_count"], 1)
        self.assertFalse(self.hint()["ready"])
        self.assertEqual(current_catalogue_scopes(self.memory.snapshot(), self.catalogue)[0]["relationship"], "unclear")

    def test_exact_current_operator_review_is_already_local_authority(self):
        item = self.hint()["pending"][0]["item"]
        accept_catalogue_scope(self.memory, self.catalogue, self.cid, item["family"]["family_id"],
            relationship="narrower", references=[item["anchor"]["observation_ref"]],
            reason="Explicit original-grounded reviewed narrowing.", reviewer="operator", as_of=waves.NOW,
            expected_meaning_hash=item["meaning_hash"])
        runner = ScopeRunner(); result = self.review(runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual(result["current_count"], 1)

    def test_no_definition_reviewed_after_cutoff_is_used_early(self):
        self.accept(dict(DEFINITION, compatibility_constraints=["Freestanding sink edge"]), at=waves.LATER)
        self.assertFalse(self.hint()["ready"])

    def test_display_correction_reuses_meaning_but_changed_meaning_needs_new_review(self):
        runner = ScopeRunner(); self.review(runner)
        self.accept(dict(DEFINITION, report_title="A corrected display title"))
        self.assertFalse(self.hint()["ready"])
        self.accept(dict(DEFINITION, compatibility_constraints=["Freestanding sink edge"]))
        self.assertTrue(self.hint()["ready"])
        self.review(runner)
        self.assertEqual(len(runner.calls), 2)

    def test_current_family_meaning_not_old_direction_heading_is_compared(self):
        runner = ScopeRunner(); self.review(runner)
        changed = deepcopy(self.catalogue)
        changed["families"][0]["definition"] += " Only countertop models."
        hint = catalogue_scope_readiness(self.memory, catalogue=changed, concept_ids=[self.cid],
            as_of=waves.NOW, runner_available=True)
        self.assertTrue(hint["ready"])
        self.assertEqual(hint["pending"][0]["item"]["family"]["scope"]["definition"], changed["families"][0]["definition"])

    def test_no_accepted_meaning_no_fake_concept_or_bridge(self):
        state = self.memory.snapshot()
        definitions = [rid for rid, row in state["source_checks"].items() if row.get("kind") == "functional-definition-v1"]
        # Read-only historic projection with no accepted definition.
        stripped = deepcopy(state)
        for rid in definitions: del stripped["source_checks"][rid]
        with patch.object(self.memory, "snapshot", return_value=stripped):
            self.assertFalse(self.hint()["ready"])
        self.assertEqual(self.memory.snapshot(), state)

    def test_scope_model_cannot_substitute_relationship_reason_or_originals(self):
        self.review(ScopeRunner())
        state = self.memory.snapshot(); row = current_catalogue_scopes(state, self.catalogue, self.cid)[0]
        with self.assertRaisesRegex(SafetyError, "DEPENDENCY_CHANGED"):
            accept_catalogue_scope(self.memory, self.catalogue, self.cid, row["catalogue"]["family_id"],
                relationship="same_scope", references=[next(r for r in row["references"] if r["table"] == "observations")],
                reason=row["reason"], reviewer="bounded_B_catalogue_scope_review", as_of=waves.NOW,
                expected_meaning_hash=row["subject"]["definition_hash"], method=METHOD,
                job_id=row["job_proof"]["job_id"], job_item_id=row["job_proof"]["item_id"])
        self.assertEqual(state, self.memory.snapshot())

    def test_changed_association_during_model_prevents_stale_scope_application(self):
        def alter():
            state = self.memory.snapshot(); child = self.children[0]; old = state["associations"][child]
            self.memory.put("associations", child, dict(old, reason="Actual association changed."), previous=old)
        result = self.review(ScopeRunner(action=alter))
        self.assertEqual(result["outcomes"][0]["state"], "waiting")
        self.assertEqual(current_catalogue_scopes(self.memory.snapshot(), self.catalogue, self.cid), [])

    def test_partial_sibling_validation_survives_and_new_batch_reuses_original(self):
        second = deepcopy(self.catalogue["families"][0]); second.update(id="caddy-second", label="Second caddy heading")
        self.catalogue["families"].append(second)
        waves.DiscoveryWaveTests.advance(self, max_actions=3, runner=FakeRunner(), admission=None)
        runner = ScopeRunner(invalid_second=True)
        result = self.review(runner)
        self.assertEqual(sum(r["state"] == "current" for r in result["outcomes"]), 1)
        self.assertEqual(sum(r["state"] == "waiting" for r in result["outcomes"]), 1)
        self.review(runner)
        self.assertEqual(len(runner.calls), 1)

    def test_pending_original_no_replacement_and_late_result_wakes_same_binding(self):
        class Uncertain:
            identity = ScopeRunner.identity
            def __init__(self): self.calls = 0
            def __call__(self, role, payload, folder): self.calls += 1; raise SafetyError("SYNTHETIC_UNCERTAIN")
        runner = Uncertain(); result = self.review(runner)
        iid = result["outcomes"][0]["binding_key"]
        hint = self.hint(); self.assertFalse(hint["ready"])
        oldwake = hint["bindings"][iid]
        self.review(runner); self.assertEqual(runner.calls, 1)
        state = self.memory.snapshot()
        jid, job = next((jid, job) for jid, job in state["jobs"].items() if job["request"]["prompt_version"] == PROMPT_VERSION)
        request = job["request"]; item = request["payload"]["items"][0]
        response, usage = ScopeRunner()("B", request["payload"], None)
        atomic_json(safe_path(self.memory.root, "ai_jobs", jid) / "result.json",
            {"request_hash": digest(request), "response": response, "usage": usage, "completed_at": waves.NOW})
        late = self.hint(skip_bindings={iid: oldwake})
        self.assertTrue(late["local_ready"])
        self.assertNotEqual(late["bindings"][iid], oldwake)
        recovered = self.review(None, skip_bindings={iid: oldwake})
        self.assertEqual(recovered["outcomes"][0]["state"], "current")
        self.assertEqual(runner.calls, 1)

    def test_wrong_model_reference_fails_without_scope_append(self):
        item = self.hint()["pending"][0]["item"]
        response, _ = ScopeRunner()("B", {"items": [item]}, None)
        response["results"][0]["references"] = ["fabricated"]
        checked = validate_results("B", {"mode": MODE, "scope_contract": CONTRACT, "items": [item]}, response,
                                   declared_version=PROMPT_VERSION)
        self.assertEqual(checked["accepted"], {})

    def test_real_work_turn_b_purpose_budget_and_cached_second_turn_zero_call(self):
        runner = ScopeRunner()
        for expected in (1, 0):
            turn = open_work_turn(self.memory, "scope-programme", at=waves.NOW,
                                 ready_lanes={"backlog": True, "exploration": False})
            try:
                with bind_work_turn(self.memory, turn["id"]):
                    result = self.review(runner)
                    self.assertEqual(result["current_count"], 1)
            finally:
                closed = close_work_turn(self.memory, turn["id"], at=waves.NOW, reason="Synthetic bounded scope turn")
            self.assertEqual(closed["counters"]["model_executions"], expected)
            if expected:
                self.assertEqual([row["purpose"] for row in closed["reservations"]["model"].values()], ["B"])
        self.assertEqual(len(runner.calls), 1)

    def test_legacy_b_version_and_cross_contract_refusal(self):
        self.assertEqual(b_contract()["version"], "research-b-functional-v2")
        item = self.hint()["pending"][0]["item"]
        payload = {"mode": MODE, "scope_contract": CONTRACT, "items": [item]}
        self.assertEqual(b_contract(payload)["version"], PROMPT_VERSION)
        for marker in ("alias_contract", "definition_contract"):
            with self.assertRaises(SafetyError): b_contract(dict(payload, **{marker: "wrong"}))
        with self.assertRaises(SafetyError): b_contract(payload, "research-b-functional-v2")
        bad = deepcopy(payload); bad["items"][0]["meaning_hash"] = "0" * 64
        with self.assertRaises(SafetyError): validate_scope_payload(bad)


if __name__ == "__main__": unittest.main()
