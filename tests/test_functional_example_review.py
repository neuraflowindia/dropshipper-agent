"""Actual saved A/B examples; fictional bounded answers and no external calls."""

from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

import test_functional_types as fixtures
from product_radar.functional_example_review import (CONTRACT, MODE, PROMPT_VERSION,
    current_example_judgments, example_review_input, functional_example_readiness,
    review_functional_examples, validate_example_review, validate_example_payload,
    validate_membership_example_review)
from product_radar.functional_types import (accept_functional_definition, current_example_memberships,
    effective_functional_definition, example_membership_preparation, functional_role_context, prepare_example_memberships)
from product_radar.models import SafetyError, canonical, digest
from product_radar.research_ai import Jobs, b_contract, current_attempt
from product_radar.research_memory import change
from product_radar.storage import atomic_json, load_json

NOW = fixtures.NOW


class Runner:
    identity = "synthetic-example-membership-v1"
    def __init__(self, *, decision="same", mutate=None): self.calls, self.decision, self.mutate = [], decision, mutate
    def __call__(self, role, payload, folder):
        self.calls.append((role, deepcopy(payload)))
        results = [{"id": item["id"], "decisions": [{"child_id": child["child_id"], "decision": self.decision,
            "reason": "Fictional judgment of this actual saved example."} for child in item["examples"]],
            "reason": "Fictional bounded example comparison, not market research."} for item in payload["items"]]
        if self.mutate: self.mutate(results)
        return {"results": results}, {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}


class FunctionalExampleReviewTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.FunctionalTypeTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        self.fixture, self.memory, self.cid = fixture, fixture.memory, fixture.cid
        self.clock = patch("product_radar.research_ai.utc_now", return_value=NOW)
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.meaning = accept_functional_definition(self.memory, self.cid, fixture.definition,
            basis_refs=fixture.refs, reviewer="synthetic_operator", reviewed_at=NOW,
            expected_meaning_hash=effective_functional_definition(self.memory.snapshot(), self.cid)["meaning_hash"],
            example_decisions={})
        self.children = example_membership_preparation(self.memory.snapshot(), self.cid)["pending_child_ids"]
        self.assertGreaterEqual(len(self.children), 2)

    def review(self, runner=None, **kwargs):
        return review_functional_examples(self.memory, [self.cid], runner, as_of=NOW, **kwargs)

    def test_grounded_preparation_and_mode_are_pure(self):
        before = self.memory.snapshot_with_provenance()
        ready = functional_example_readiness(self.memory, [self.cid], as_of=NOW)
        self.assertTrue(ready["model_ready"]); self.assertFalse(ready["local_ready"])
        self.assertEqual(before, self.memory.snapshot_with_provenance())
        item = ready["prepared"][0]["item"]
        self.assertEqual([row["child_id"] for row in item["examples"]], self.children)
        payload = {"mode": MODE, "example_contract": CONTRACT, "items": [item]}
        validate_example_payload(payload); self.assertEqual(b_contract(payload)["version"], PROMPT_VERSION)
        for marker in ("scope_contract", "definition_contract", "alias_contract"):
            with self.assertRaises(SafetyError): b_contract(dict(payload, **{marker: "wrong"}))

    def test_actual_saved_examples_are_judged_without_redefining_or_reassociating(self):
        before = self.memory.snapshot(); runner = Runner(); result = self.review(runner)
        self.assertFalse(result["failures"]); self.assertIn(self.cid, result["accepted"])
        self.assertEqual(len(runner.calls), 1)
        after = self.memory.snapshot()
        for table in ("concepts", "associations", "assessments", "observations", "captures", "published_history"):
            self.assertEqual(after[table], before[table], table)
        self.assertEqual(after["source_checks"][self.meaning["definition_id"]], before["source_checks"][self.meaning["definition_id"]])
        prepare_example_memberships(self.memory, self.cid, as_of=NOW)
        members = current_example_memberships(self.memory.snapshot(), self.cid)
        self.assertEqual({row["child_id"] for row in members}, set(self.children))
        state = self.memory.snapshot()
        for member in members:
            saved = state["source_checks"][member["membership_id"]]
            ref = saved["example_review"]
            validate_membership_example_review(saved, state["source_checks"][ref["id"]], as_of=NOW)
        role = functional_role_context(state, self.cid, role="E")
        self.assertTrue(all(entry.get("example_review_id") for entries in role["proof"]["examples"].values() for entry in entries))
        before = self.memory.snapshot_with_provenance(); repeated = self.review(runner)
        self.assertIn(self.cid, repeated["accepted"]); self.assertEqual(len(runner.calls), 1)
        self.assertEqual(before, self.memory.snapshot_with_provenance())

    def assert_negative_cached(self, decision):
        runner = Runner(decision=decision); result = self.review(runner)
        self.assertFalse(result["failures"])
        prepare_example_memberships(self.memory, self.cid, as_of=NOW)
        self.assertEqual(current_example_memberships(self.memory.snapshot(), self.cid), [])
        self.assertFalse(functional_example_readiness(self.memory, [self.cid], as_of=NOW)["ready"])
        self.review(runner); self.assertEqual(len(runner.calls), 1)

    def test_unclear_is_cached_without_false_membership_or_repeat_AI(self): self.assert_negative_cached("unclear")

    def test_different_is_cached_without_false_membership_or_repeat_AI(self): self.assert_negative_cached("different")

    def test_changed_association_proof_reuses_exact_unchanged_semantic_answer(self):
        runner = Runner(); first = self.review(runner)
        state = self.memory.snapshot(); child = self.children[0]; previous = state["associations"][child]
        self.memory.put("associations", child, dict(previous, reason="Corrected association explanation only."), previous=previous)
        ready = functional_example_readiness(self.memory, [self.cid], as_of=NOW)
        self.assertTrue(ready["local_ready"]); self.assertFalse(ready["model_ready"])
        second = self.review(None)
        self.assertFalse(second["failures"]); self.assertEqual(len(runner.calls), 1)
        self.assertNotEqual(first["accepted"][self.cid]["review_id"], second["accepted"][self.cid]["review_id"])
        prepare_example_memberships(self.memory, self.cid, as_of=NOW)
        self.assertEqual(len(current_example_memberships(self.memory.snapshot(), self.cid)), len(self.children))

    def test_unknown_original_does_not_own_new_disjoint_children_of_the_same_type(self):
        from test_research_flow import FakeRunner, capture, run_flow
        class Unknown(Runner):
            def __call__(self, role, payload, folder):
                self.calls.append((role, deepcopy(payload))); raise SafetyError("SYNTHETIC_ORIGINAL_UNKNOWN")
        unknown = Unknown(); self.review(unknown)
        self.assertEqual(len(unknown.calls), 1)
        self.assertFalse(functional_example_readiness(self.memory, [self.cid], as_of=NOW)["ready"])
        capture(self.memory, [{"ad_archive_id": "1101", "page_id": "1201", "is_active": True,
            "start_date": 1788220800, "snapshot": {"title": "Another sink caddy"}}], acquisition="new-example")
        run_flow(self.memory, FakeRunner(), as_of=NOW)
        new_children = set(example_membership_preparation(self.memory.snapshot(), self.cid)["pending_child_ids"]) - set(self.children)
        self.assertEqual(len(new_children), 1)
        ready = functional_example_readiness(self.memory, [self.cid], as_of=NOW)
        self.assertTrue(ready["model_ready"])
        self.assertEqual({row["child_id"] for row in ready["prepared"][0]["item"]["examples"]}, new_children)
        runner = Runner(); result = self.review(runner)
        self.assertFalse(result["failures"]); self.assertEqual(len(runner.calls), 1); self.assertEqual(len(unknown.calls), 1)
        self.assertEqual(set(current_example_judgments(self.memory.snapshot(), self.cid, self.meaning["meaning_hash"])), new_children)

    def test_more_than_twenty_actual_children_continue_in_bounded_original_batches(self):
        from test_research_flow import FakeRunner, capture, run_flow
        values = [{"ad_archive_id": str(3000 + index), "page_id": str(4000 + index), "is_active": True,
            "start_date": 1788220800, "snapshot": {"title": "Fictional sink caddy variation " + str(index)}}
            for index in range(21)]
        capture(self.memory, values, acquisition="many-actual-examples")
        source_runner = FakeRunner()
        for _ in range(3): run_flow(self.memory, source_runner, as_of=NOW)
        all_children = example_membership_preparation(self.memory.snapshot(), self.cid)["pending_child_ids"]
        self.assertEqual(len(all_children), 23)
        runner = Runner(); first = self.review(runner)
        self.assertFalse(first["failures"])
        self.assertEqual(len(runner.calls[0][1]["items"][0]["examples"]), 20)
        second = self.review(runner)
        self.assertFalse(second["failures"])
        self.assertEqual(len(runner.calls[1][1]["items"][0]["examples"]), 3)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(set(current_example_judgments(self.memory.snapshot(), self.cid, self.meaning["meaning_hash"])), set(all_children))
        self.assertFalse(functional_example_readiness(self.memory, [self.cid], as_of=NOW)["ready"])

    def test_child_input_packing_splits_without_truncating_real_examples(self):
        import product_radar.functional_example_review as module
        state = self.memory.snapshot()
        one = example_review_input(state, self.cid, self.children[:1], as_of=NOW)
        two = example_review_input(state, self.cid, self.children[:2], as_of=NOW)
        payload = lambda item: {"mode": MODE, "example_contract": CONTRACT, "items": [item]}
        cap = (len(canonical(payload(one["item"]))) + len(canonical(payload(two["item"])))) // 2
        runner = Runner()
        with patch.object(module, "MAX_INPUT_BYTES", cap):
            first = self.review(runner); second = self.review(runner)
        self.assertFalse(first["failures"]); self.assertFalse(second["failures"])
        self.assertEqual(len(runner.calls), 2)
        self.assertTrue(all(len(call[1]["items"][0]["examples"]) == 1 for call in runner.calls))
        self.assertEqual(set(current_example_judgments(self.memory.snapshot(), self.cid, self.meaning["meaning_hash"])), set(self.children))

    def test_skipped_binding_wakes_only_on_saved_input_or_original_file_change(self):
        first = functional_example_readiness(self.memory, [self.cid], as_of=NOW)
        skip = first["bindings"]
        later = functional_example_readiness(self.memory, [self.cid], as_of="2026-10-06T10:00:00+00:00",
            skip_concept_bindings=skip)
        self.assertEqual(first["bindings"], later["bindings"]); self.assertFalse(later["ready"])
        state = self.memory.snapshot(); child = self.children[0]; previous = state["associations"][child]
        self.memory.put("associations", child, dict(previous, reason="Changed actual association record."), previous=previous)
        changed = functional_example_readiness(self.memory, [self.cid], as_of=NOW, skip_concept_bindings=skip)
        self.assertTrue(changed["ready"]); self.assertNotEqual(first["bindings"], changed["bindings"])

    def test_delayed_review_cannot_create_backdated_memberships(self):
        later = "2026-10-05T10:01:00+00:00"
        runner = Runner()
        with patch("product_radar.research_ai.utc_now", return_value=later):
            result = self.review(runner)
        self.assertFalse(result["failures"])
        waiting = functional_example_readiness(self.memory, [self.cid], as_of=NOW)
        self.assertFalse(waiting["ready"]); self.assertEqual(waiting["next_retry_at"], later)
        self.assertEqual(waiting["waits"], [later])
        self.review(runner); self.assertEqual(len(runner.calls), 1)
        due = functional_example_readiness(self.memory, [self.cid], as_of=later,
            skip_concept_bindings=waiting["bindings"])
        self.assertIn(self.cid, due["current"])
        self.assertNotEqual(waiting["bindings"], due["bindings"])
        self.assertFalse(current_example_judgments(self.memory.snapshot(), self.cid, self.meaning["meaning_hash"], as_of=NOW))
        before = prepare_example_memberships(self.memory, self.cid, as_of=NOW)
        self.assertFalse(before["membership_ids"])
        after = prepare_example_memberships(self.memory, self.cid, as_of=later)
        self.assertTrue(after["membership_ids"])
        state = self.memory.snapshot(); member = deepcopy(state["source_checks"][after["membership_ids"][0]])
        review = state["source_checks"][member["example_review"]["id"]]
        member["prepared_at"] = NOW
        with self.assertRaises(SafetyError): validate_membership_example_review(member, review, as_of=later)

    def test_same_semantic_recapture_adds_exact_originals_without_another_semantic_call(self):
        from test_research_flow import capture
        runner = Runner(); self.review(runner)
        state = self.memory.snapshot(); rid, row = next((rid, row) for rid, row in state["source_checks"].items() if row.get("kind") == CONTRACT)
        child = self.children[0]; oid = row["binding"]["children"][child]["observation_id"]
        observation = state["observations"][oid]
        updated = deepcopy(observation); updated["facts"]["price"] = "299"
        with self.assertRaisesRegex(SafetyError, "RESEARCH_CAPTURE_CONFLICT"):
            self.memory.put("observations", oid, updated, previous=observation)
        capture(self.memory, acquisition="same-semantic-recapture", observed="2026-10-05T09:00:00+00:00")
        judgment = current_example_judgments(self.memory.snapshot(), self.cid, self.meaning["meaning_hash"])
        self.assertIn(child, judgment)
        ready = functional_example_readiness(self.memory, [self.cid], as_of=NOW)
        self.assertFalse(ready["ready"])
        result = self.review(None)
        self.assertFalse(result["failures"]); self.assertEqual(len(runner.calls), 1)
        prepare_example_memberships(self.memory, self.cid, as_of=NOW)
        for member in current_example_memberships(self.memory.snapshot(), self.cid):
            record = self.memory.snapshot()["source_checks"][member["membership_id"]]
            validate_membership_example_review(record,
                self.memory.snapshot()["source_checks"][record["example_review"]["id"]], as_of=NOW)
            self.assertGreater(len(record["observation_hashes"]), 1)

    def test_a_known_explicit_definition_decision_is_not_overridden_automatically(self):
        state = self.memory.snapshot()
        decisions = {child: {"decision": "different", "reason": "Explicit synthetic distinction."} for child in self.children}
        accept_functional_definition(self.memory, self.cid, self.fixture.definition, basis_refs=self.fixture.refs,
            reviewer="synthetic_operator", reviewed_at=NOW,
            expected_meaning_hash=effective_functional_definition(state, self.cid)["meaning_hash"], example_decisions=decisions)
        runner = Runner(); result = self.review(runner)
        self.assertEqual(runner.calls, []); self.assertFalse(result["failures"])
        self.assertEqual(current_example_memberships(self.memory.snapshot(), self.cid), [])

    def test_later_same_meaning_explicit_different_is_not_overridden_by_old_same_review(self):
        runner = Runner(); self.review(runner)
        prepare_example_memberships(self.memory, self.cid, as_of=NOW)
        self.assertTrue(current_example_memberships(self.memory.snapshot(), self.cid))
        state = self.memory.snapshot(); before = effective_functional_definition(state, self.cid)
        decisions = {child: {"decision": "different", "reason": "Later explicit synthetic distinction."} for child in self.children}
        accept_functional_definition(self.memory, self.cid, self.fixture.definition, basis_refs=self.fixture.refs,
            reviewer="synthetic_operator", reviewed_at=NOW, expected_meaning_hash=before["meaning_hash"],
            example_decisions=decisions)
        after = effective_functional_definition(self.memory.snapshot(), self.cid)
        self.assertEqual(before["meaning_hash"], after["meaning_hash"])
        self.assertNotEqual(before["definition_id"], after["definition_id"])
        self.assertFalse(current_example_judgments(self.memory.snapshot(), self.cid, after["meaning_hash"]))
        self.assertFalse(current_example_memberships(self.memory.snapshot(), self.cid))
        self.review(runner); self.assertEqual(len(runner.calls), 1)

    def test_never_started_stale_original_is_a_named_wait_not_new_model_authority(self):
        import product_radar.research_ai as ai
        ready = functional_example_readiness(self.memory, [self.cid], as_of=NOW)
        item = ready["prepared"][0]["item"]; runner = Runner(); write = ai.atomic_json
        def stop(path, value):
            if Path(path).name == "intent.json": raise OSError("Fictional interruption before intent")
            return write(path, value)
        with patch.object(ai, "atomic_json", side_effect=stop):
            with self.assertRaises(OSError): Jobs(self.memory, runner).run("B", {"mode": MODE, "example_contract": CONTRACT, "items": [item]})
        state = self.memory.snapshot(); child = self.children[0]; previous = state["associations"][child]
        self.memory.put("associations", child, dict(previous, status="deferred"), previous=previous)
        waiting = functional_example_readiness(self.memory, [self.cid], as_of=NOW)
        self.assertFalse(waiting["ready"])
        self.assertEqual(waiting["waiting"][self.cid]["reason"], "FUNCTIONAL_EXAMPLE_ORIGINAL_BATCH_INPUT_CHANGED")
        result = self.review(runner)
        self.assertEqual(result["failures"][self.cid]["reason"], "FUNCTIONAL_EXAMPLE_ORIGINAL_BATCH_INPUT_CHANGED")
        self.assertEqual(runner.calls, [])

    def test_original_answer_survives_a_save_failure_and_uses_no_second_model_call(self):
        commit = self.memory.commit; runner = Runner()
        def stop(eid, changes, *, validate=None):
            if eid.startswith("save-functional-example-review"): raise OSError("Fictional interrupted application")
            return commit(eid, changes, validate=validate)
        with patch.object(self.memory, "commit", side_effect=stop): first = self.review(runner)
        self.assertEqual(first["failures"][self.cid]["reason"], "FUNCTIONAL_EXAMPLE_REVIEW_SAVE_FAILED")
        ready = functional_example_readiness(self.memory, [self.cid], as_of=NOW)
        self.assertTrue(ready["local_ready"])
        result = self.review(None)
        self.assertFalse(result["failures"]); self.assertEqual(len(runner.calls), 1)

    def test_saved_capsule_survives_missing_temporary_original_and_resealing_cannot_change_decision(self):
        self.review(Runner(decision="different")); state = self.memory.snapshot()
        rid, row = next((rid, row) for rid, row in state["source_checks"].items() if row.get("kind") == CONTRACT)
        (current_attempt(self.memory.root / "ai_jobs" / row["job_id"]) / "result.json").unlink()
        self.assertTrue(current_example_judgments(self.memory.snapshot(), self.cid, self.meaning["meaning_hash"]))
        bad = deepcopy(row); bad["result"]["decisions"][0]["decision"] = "same"
        bad["record_hash"] = digest({k: v for k, v in bad.items() if k != "record_hash"})
        with self.assertRaises(SafetyError): validate_example_review(rid, bad)

    def test_original_result_file_is_pinned_inside_commit(self):
        commit = self.memory.commit; injected = False
        def race(eid, changes, *, validate=None):
            nonlocal injected
            if eid.startswith("save-functional-example-review") and not injected:
                injected = True; row = changes[0]["value"]
                path = current_attempt(self.memory.root / "ai_jobs" / row["job_id"]) / "result.json"
                value = load_json(path); value["response"]["results"][0]["reason"] = "Changed actual reply"
                atomic_json(path, value)
            return commit(eid, changes, validate=validate)
        with patch.object(self.memory, "commit", side_effect=race): result = self.review(Runner())
        self.assertIn("ORIGINAL_RESULT_CHANGED", result["failures"][self.cid]["reason"])
        self.assertFalse(any(row.get("kind") == CONTRACT for row in self.memory.snapshot()["source_checks"].values()))

    def test_partial_saved_original_recovers_only_current_children(self):
        import product_radar.functional_example_review as module
        runner = Runner(); commit = self.memory.commit
        def stop(eid, changes, *, validate=None):
            if eid.startswith("save-functional-example-review"): raise OSError("Fictional interruption")
            return commit(eid, changes, validate=validate)
        with patch.object(self.memory, "commit", side_effect=stop): self.review(runner)
        state = self.memory.snapshot(); stale = self.children[0]; previous = state["associations"][stale]
        self.memory.put("associations", stale, dict(previous, status="deferred"), previous=previous)
        result = self.review(None); self.assertFalse(result["failures"]); self.assertEqual(len(runner.calls), 1)
        saved = self.memory.snapshot()["source_checks"][result["accepted"][self.cid]["review_id"]]
        self.assertEqual(set(saved["binding"]["children"]), set(self.children[1:]))
        self.assertEqual(len(saved["job_proof"]["item"]["examples"]), len(self.children))
        self.assertEqual(set(current_example_judgments(self.memory.snapshot(), self.cid, self.meaning["meaning_hash"])), set(self.children[1:]))


if __name__ == "__main__": unittest.main()
