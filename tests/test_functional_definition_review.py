"""Real captured A/B fixtures and fictional definition answers; zero live calls."""

from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

import test_functional_types as fixtures
from product_radar.functional_definition_review import (CONTRACT, MODE, PROMPT_VERSION, PROOF_KEY,
    definition_review_input, functional_definition_readiness, prepare_functional_definition_reviews,
    review_functional_definitions, validate_definition_acceptance_proof, validate_definition_item,
    validate_definition_payload, validate_definition_result)
from product_radar.functional_types import (accept_functional_definition, effective_functional_definition,
    prepare_example_memberships, current_example_memberships, _verified_definition)
from product_radar.models import SafetyError, digest
from product_radar.research_ai import (B_PROMPT_VERSION, Jobs, b_contract, current_attempt,
    instructions_for, prompt_version, schema_for, validate_results)
from product_radar.research_memory import change, key
from product_radar.storage import atomic_json, load_json
from product_radar.work_turns import DEFAULT_POLICY, bind_work_turn, open_work_turn

NOW = fixtures.NOW


class Runner:
    identity = "synthetic-functional-definition-v1"
    def __init__(self, *, decision="prepared", mutate=None):
        self.calls, self.decision, self.mutate = [], decision, mutate
    def __call__(self, role, payload, folder):
        self.calls.append((role, deepcopy(payload)))
        rows = []
        for item in payload["items"]:
            description = item["examples"][0]["description"]
            rows.append({"id": item["id"], "decision": self.decision,
                "definition": {"report_title": description["label"], "primary_use": description["function"],
                    "mechanism": description["mechanism"], "use_context": [], "compatibility_constraints": [],
                    "included_distinctions": description["features"], "excluded_distinctions": []}
                    if self.decision == "prepared" else None,
                "references": [item["examples"][0]["child_id"]] if self.decision == "prepared" else [],
                "example_decisions": [{"child_id": example["child_id"],
                    "decision": "same" if self.decision == "prepared" else "unclear",
                    "reason": "Explicit fictional membership judgment from the supplied example."}
                    for example in item["examples"]],
                "reason": "Fictional bounded definition judgment, not a real product recommendation."})
        if self.mutate: self.mutate(rows)
        return {"results": rows}, {"input_tokens": 20, "cached_input_tokens": 0, "output_tokens": 10}


class FunctionalDefinitionReviewTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.FunctionalTypeTests(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.memory = fixture, fixture.memory
        self.cids = sorted(self.memory.snapshot()["concepts"])
        self.clock = patch("product_radar.research_ai.utc_now", return_value=NOW)
        self.clock.start(); self.addCleanup(self.clock.stop)

    def run_review(self, runner=None, ids=None):
        return review_functional_definitions(self.memory, ids or self.cids, runner, as_of=NOW)

    def definition(self, cid=None):
        state = self.memory.snapshot()
        current = effective_functional_definition(state, cid or self.cids[0])
        return current, state["source_checks"].get(current["definition_id"])

    def spent_turn(self):
        from test_research_flow import FakeRunner
        policy = deepcopy(DEFAULT_POLICY)
        policy.update(model_executions=1, report_slots=0)
        turn = open_work_turn(self.memory, "definition-preparation-limit", at=NOW, policy=policy)
        with bind_work_turn(self.memory, turn["id"]):
            Jobs(self.memory, FakeRunner()).run("A", {"items": [
                {"id": "spent-slot", "source_text": {"title": "Unclear offer"}}]})
        return turn

    def test_spent_fresh_definition_scope_is_prepared_once_and_reads_history_once(self):
        import product_radar.functional_definition_review as module
        turn = self.spent_turn()
        before = self.memory.snapshot_with_provenance()
        runner = Runner()
        with bind_work_turn(self.memory, turn["id"]), \
                patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as reads, \
                patch.object(module, "definition_review_input", wraps=definition_review_input) as preparations, \
                patch.object(Jobs, "run", side_effect=AssertionError("No new definition has room in this turn")):
            result = self.run_review(runner)
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(preparations.call_count, len(self.cids))
        self.assertEqual(result["prepared_count"], 0)
        self.assertEqual({row["reason"] for row in result["failures"].values()}, {"WORK_TURN_MODEL_LIMIT"})
        self.assertEqual(set(result["bindings"]), set(self.cids))
        self.assertEqual(self.memory.snapshot_with_provenance(), before)
        self.assertEqual(runner.calls, [])

    def test_spent_turn_still_applies_saved_original_definition_answers(self):
        import product_radar.functional_definition_review as module
        original_runner = Runner()
        with patch.object(module, "accept_functional_definition", side_effect=OSError("Synthetic save interruption")):
            first = self.run_review(original_runner)
        self.assertFalse(first["accepted"])
        self.assertEqual(len(original_runner.calls), 1)
        turn = self.spent_turn()
        jobs = set(self.memory.snapshot()["jobs"])
        other = Runner(); other.identity = "different-current-runner"
        with bind_work_turn(self.memory, turn["id"]):
            result = self.run_review(other)
        state = self.memory.snapshot()
        self.assertEqual(set(result["accepted"]), set(self.cids))
        self.assertFalse(result["failures"])
        self.assertEqual(set(state["jobs"]), jobs)
        self.assertEqual(state["batch_runs"][turn["id"]]["counters"]["model_executions"], 1)
        self.assertEqual(other.calls, [])

    def test_existing_unstarted_definition_service_can_run_at_its_original_ceiling(self):
        from product_radar.research_ai import MODEL, _item_bindings
        from product_radar.work_turns import admit_model_job
        runner = Runner()
        prepared = prepare_functional_definition_reviews(self.memory.snapshot(), self.cids, as_of=NOW)
        payload = {"mode": MODE, "definition_contract": CONTRACT,
                   "items": [row["item"] for row in prepared["prepared"]]}
        request = {"role": "B", "payload": payload, "model": MODEL,
                   "prompt_version": PROMPT_VERSION, "runner": runner.identity}
        bindings = _item_bindings("B", payload, MODEL, PROMPT_VERSION, runner.identity)
        policy = deepcopy(DEFAULT_POLICY); policy.update(model_executions=1, report_slots=0)
        turn = open_work_turn(self.memory, "existing-definition-service", at=NOW, policy=policy)
        with bind_work_turn(self.memory, turn["id"]):
            admit_model_job(self.memory, key("job", request), request, bindings, attempt="original", at=NOW)
            result = self.run_review(runner)
        self.assertEqual(set(result["accepted"]), set(self.cids))
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["model_executions"], 1)

    def test_due_definition_review_keeps_its_protected_share_of_the_turn(self):
        from test_research_flow import FakeRunner
        policy = deepcopy(DEFAULT_POLICY); policy.update(model_executions=2, report_slots=0)
        def ordinary(turn, item_id):
            with bind_work_turn(self.memory, turn["id"]):
                return Jobs(self.memory, FakeRunner()).run("A", {"items": [
                    {"id": item_id, "source_text": {"title": "Ordinary discovery offer " + item_id}}]})
        # Without due review work, ordinary questions may spend the whole turn.
        idle = open_work_turn(self.memory, "definition-share-idle", at=NOW, policy=policy)
        ordinary(idle, "idle-1"); ordinary(idle, "idle-2")
        with bind_work_turn(self.memory, idle["id"]):
            starved = self.run_review(Runner())
        self.assertEqual({row["reason"] for row in starved["failures"].values()}, {"WORK_TURN_MODEL_LIMIT"})
        # With due review work, the second execution is kept for the definition.
        due = open_work_turn(self.memory, "definition-share-due", at=NOW, policy=policy, review_ready=True)
        ordinary(due, "due-1")
        with self.assertRaisesRegex(SafetyError, "WORK_TURN_MODEL_LIMIT"):
            ordinary(due, "due-2")
        runner = Runner()
        with bind_work_turn(self.memory, due["id"]):
            result = self.run_review(runner)
        self.assertEqual(set(result["accepted"]), set(self.cids))
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.memory.snapshot()["batch_runs"][due["id"]]["counters"]["model_executions"], 2)

    def test_legacy_B_and_alias_mode_remain_separate_from_definition_mode(self):
        old = {"items": [{"id": "child", "candidates": []}]}
        self.assertEqual(prompt_version("B", old), B_PROMPT_VERSION)
        prepared = definition_review_input(self.memory.snapshot(), self.cids[0], as_of=NOW)
        payload = {"mode": MODE, "definition_contract": CONTRACT, "items": [prepared["item"]]}
        self.assertEqual(prompt_version("B", payload), PROMPT_VERSION)
        self.assertIn("definition", schema_for("B", payload)["properties"]["results"]["items"]["properties"])
        self.assertIn("actual supplied example", instructions_for("B", payload))
        with self.assertRaises(SafetyError): b_contract(payload, B_PROMPT_VERSION)
        with self.assertRaises(SafetyError): b_contract(old, PROMPT_VERSION)
        with self.assertRaises(SafetyError): b_contract(dict(payload, alias_contract="functional-alias-review-v1"))

    def test_preparation_is_pure_grounded_and_does_not_invent_concepts(self):
        before = self.memory.snapshot_with_provenance()
        result = prepare_functional_definition_reviews(before["state"], self.cids, as_of=NOW)
        self.assertEqual(len(result["prepared"]), len(self.cids))
        self.assertEqual(before, self.memory.snapshot_with_provenance())
        row = result["prepared"][0]
        for child, binding in row["binding"]["children"].items():
            self.assertEqual(before["state"]["associations"][child]["concept_id"], row["concept_id"])
            self.assertEqual(digest(before["state"]["observations"][binding["observation_id"]]), binding["observation_hash"])
        bad = prepare_functional_definition_reviews(before["state"], ["invented"], as_of=NOW)
        self.assertEqual(bad["waiting"]["invented"]["reason"], "FUNCTIONAL_CONCEPT_NOT_FOUND")
        with self.assertRaises(SafetyError): prepare_functional_definition_reviews(before["state"], [[]], as_of=NOW)

    def test_actual_A_B_examples_become_accepted_definitions_without_identity_or_score_changes(self):
        before = self.memory.snapshot(); runner = Runner()
        result = self.run_review(runner)
        self.assertEqual(set(result["accepted"]), set(self.cids)); self.assertFalse(result["failures"])
        self.assertEqual(len(runner.calls), 1)
        self.assertFalse(result["client_publication_attempted"])
        after = self.memory.snapshot()
        for table in ("concepts", "associations", "assessments", "observations", "captures", "published_history", "issue_reservations"):
            self.assertEqual(after[table], before[table])
        for cid in self.cids:
            current, row = self.definition(cid)
            self.assertTrue(current["accepted"])
            proof = row["job_proof"][PROOF_KEY]
            self.assertEqual(proof["job_id"], row["job_id"])
            self.assertEqual(validate_definition_acceptance_proof(proof, concept_id=cid, definition=row["definition"],
                example_decisions=row["example_decisions"], basis_refs=row["basis_refs"])["decision"], "prepared")
            prepare_example_memberships(self.memory, cid, as_of=NOW)
            self.assertTrue(current_example_memberships(self.memory.snapshot(), cid))

    def test_current_accepted_meaning_reuses_locally_without_a_second_model_job(self):
        runner = Runner(); self.run_review(runner)
        before = self.memory.snapshot_with_provenance()
        result = self.run_review(Runner())
        self.assertEqual(set(result["accepted"]), set(self.cids))
        self.assertEqual(before, self.memory.snapshot_with_provenance())
        self.assertEqual(len(runner.calls), 1)

    def test_operator_accepted_definition_is_reused_without_silently_promoting_legacy(self):
        self.fixture.accept()
        runner = Runner()
        result = self.run_review(runner, [self.fixture.cid])
        self.assertTrue(result["accepted"][self.fixture.cid]["state"] == "current")
        self.assertEqual(runner.calls, [])

    def test_unclear_is_saved_once_and_does_not_force_a_definition_or_retry_model(self):
        runner = Runner(decision="unclear")
        first = self.run_review(runner); before = self.memory.snapshot_with_provenance()
        self.assertFalse(first["accepted"])
        self.assertTrue(all(row["reason"] == "FUNCTIONAL_DEFINITION_UNCLEAR" for row in first["failures"].values()))
        self.assertFalse(functional_definition_readiness(self.memory, self.cids, as_of=NOW)["ready"])
        self.run_review(runner)
        self.assertEqual(len(runner.calls), 1); self.assertEqual(before, self.memory.snapshot_with_provenance())
        self.assertTrue(all(not effective_functional_definition(before["state"], cid)["accepted"] for cid in self.cids))

    def test_unknown_original_keeps_ownership_and_does_not_starve_an_independent_type(self):
        class Unknown(Runner):
            def __call__(self, role, payload, folder):
                self.calls.append((role, payload)); raise SafetyError("SYNTHETIC_EXECUTION_UNKNOWN")
        unknown = Unknown(); first = self.run_review(unknown, [self.cids[0]])
        self.assertFalse(first["accepted"]); self.assertEqual(len(unknown.calls), 1)
        ready = functional_definition_readiness(self.memory, self.cids, as_of=NOW)
        self.assertEqual(ready["ready_concept_ids"], [self.cids[1]])
        runner = Runner(); second = self.run_review(runner)
        self.assertEqual(set(second["accepted"]), {self.cids[1]})
        self.assertEqual(second["failures"][self.cids[0]]["reason"], "AI_JOB_PENDING_RECONCILIATION")
        self.assertEqual(len(runner.calls), 1); self.assertEqual(len(unknown.calls), 1)

    def test_unchanged_readiness_has_no_clock_only_busy_loop(self):
        first = functional_definition_readiness(self.memory, self.cids, as_of=NOW)
        second = functional_definition_readiness(self.memory, self.cids, as_of="2026-10-06T10:00:00+00:00")
        self.assertEqual(first["signal"], second["signal"])

    def test_skipped_unchanged_failure_does_not_occupy_the_next_ready_slot(self):
        first = functional_definition_readiness(self.memory, self.cids, as_of=NOW, limit=1)
        skipped = {self.cids[0]: first["bindings"][self.cids[0]]}
        ready = functional_definition_readiness(self.memory, self.cids, as_of=NOW, limit=1,
            skip_concept_bindings=skipped)
        self.assertEqual(ready["ready_concept_ids"], [self.cids[1]])
        self.assertEqual(ready["skipped_concept_ids"], [self.cids[0]])
        self.assertEqual(set(ready["bindings"]), set(self.cids))
        runner = Runner()
        result = review_functional_definitions(self.memory, self.cids, runner, as_of=NOW, limit=1,
            skip_concept_bindings=skipped)
        self.assertEqual(set(result["accepted"]), {self.cids[1]})
        self.assertEqual(set(result["bindings"]), set(self.cids))
        self.assertEqual(len(runner.calls), 1)

    def test_full_actual_scope_can_progress_past_five_deferred_types(self):
        from test_research_flow import FakeRunner, capture, run_flow
        class DistinctExamples(FakeRunner):
            def __call__(self, role, payload, folder):
                result, usage = super().__call__(role, payload, folder)
                if role == "A":
                    for item, row in zip(payload["items"], result["results"]):
                        for product in row["products"]:
                            product["label"] = item["source_text"]["title"]
                            product["function"] = "Carry out " + product["label"]
                return result, usage
        values = [{"ad_archive_id": str(1000 + index), "page_id": str(2000 + index), "is_active": True,
            "start_date": 1788220800, "snapshot": {"title": "Fictional practical type " + str(index)}}
            for index in range(6)]
        capture(self.memory, values, acquisition="additional-definition-examples")
        run_flow(self.memory, DistinctExamples(), as_of=NOW)
        ids = sorted(self.memory.snapshot()["concepts"])
        self.assertEqual(len(ids), 8)
        first = review_functional_definitions(self.memory, ids, None, as_of=NOW)
        self.assertEqual(first["prepared_count"], 5)
        self.assertEqual(len(first["failures"]), 5)
        skipped = {cid: first["bindings"][cid] for cid in first["failures"]}
        ready = functional_definition_readiness(self.memory, ids, as_of=NOW, skip_concept_bindings=skipped)
        self.assertEqual(ready["ready_concept_ids"], ids[5:])
        runner = Runner()
        result = review_functional_definitions(self.memory, ids, runner, as_of=NOW,
            skip_concept_bindings=skipped)
        self.assertEqual(set(result["accepted"]), set(ids[5:]))
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(set(result["bindings"]), set(ids))

    def test_every_current_invalid_and_unclear_subject_has_a_stable_binding(self):
        self.run_review(Runner(decision="unclear"), [self.cids[0]])
        self.run_review(Runner(), [self.cids[1]])
        ids = self.cids + ["not-an-existing-concept"]
        first = functional_definition_readiness(self.memory, ids, as_of=NOW)
        later = functional_definition_readiness(self.memory, ids, as_of="2026-10-06T10:00:00+00:00")
        self.assertEqual(set(first["bindings"]), set(ids))
        self.assertEqual(first["bindings"], later["bindings"])
        self.assertFalse(first["ready"])
        with self.assertRaises(SafetyError):
            functional_definition_readiness(self.memory, ids, as_of=NOW, skip_concept_bindings={ids[0]: "bad-hash"})

    def test_known_established_or_inactive_subjects_do_not_create_definition_jobs(self):
        from product_radar.research_flow import exclude_established_brand
        cid = self.cids[0]; state = self.memory.snapshot()
        exclude_established_brand(self.memory, {"concept_id": cid,
            "expected_description_hash": digest(state["concepts"][cid]["description"]),
            "reason": "Fictional source-grounded exclusion for this test.",
            "source_url": "https://example.com/brands", "source_excerpt": "Fictional established brand entry.",
            "reviewer": "synthetic_operator", "reviewed_at": NOW})
        runner = Runner(); result = self.run_review(runner, [cid])
        self.assertEqual(runner.calls, [])
        self.assertEqual(result["failures"][cid]["reason"], "FUNCTIONAL_DEFINITION_NOT_APPLICABLE")
        original = self.memory.snapshot()["concepts"][self.cids[1]]
        self.memory.put("concepts", self.cids[1], dict(original, status="retired"), previous=original)
        result = self.run_review(runner, [self.cids[1]])
        self.assertEqual(runner.calls, [])
        self.assertEqual(result["failures"][self.cids[1]]["reason"], "FUNCTIONAL_CONCEPT_INACTIVE")

    def test_late_original_reply_changes_skipped_binding_and_recovers_without_a_model_call(self):
        import product_radar.functional_definition_review as module
        runner = Runner()
        with patch.object(module, "accept_functional_definition", side_effect=OSError("Fictional interruption")):
            result = self.run_review(runner, [self.cids[0]])
        jid = next(jid for jid, job in self.memory.snapshot()["jobs"].items()
            if job["request"].get("prompt_version") == PROMPT_VERSION)
        folder = current_attempt(self.memory.root / "ai_jobs" / jid)
        held = {}
        for name in ("result.json", "completion.json"):
            path = folder / name
            if path.exists(): held[name] = path.read_bytes(); path.unlink()
        waiting = functional_definition_readiness(self.memory, [self.cids[0]], as_of=NOW)
        self.assertFalse(waiting["ready"])
        skip = waiting["bindings"]
        self.assertFalse(functional_definition_readiness(self.memory, [self.cids[0]], as_of=NOW,
            skip_concept_bindings=skip)["ready"])
        for name, content in held.items(): (folder / name).write_bytes(content)
        ready = functional_definition_readiness(self.memory, [self.cids[0]], as_of=NOW,
            skip_concept_bindings=skip)
        self.assertTrue(ready["local_ready"]); self.assertFalse(ready["model_ready"])
        self.assertNotEqual(ready["bindings"], skip)
        recovered = review_functional_definitions(self.memory, [self.cids[0]], None, as_of=NOW,
            skip_concept_bindings=skip)
        self.assertIn(self.cids[0], recovered["accepted"])
        self.assertEqual(len(runner.calls), 1)

    def test_real_delayed_reply_is_accepted_at_its_completion_not_the_round_start(self):
        completed = "2026-10-05T12:01:00+00:00"
        from product_radar.models import timestamp
        self.assertGreater(timestamp(completed), timestamp(NOW))
        with patch("product_radar.research_ai.utc_now", return_value=completed):
            result = self.run_review(Runner(), [self.cids[0]])
        self.assertIn(self.cids[0], result["accepted"])
        _, record = self.definition()
        self.assertEqual(record["reviewed_at"], completed)
        self.assertEqual(record["job_proof"][PROOF_KEY]["result_receipt"]["completed_at"], completed)
        damaged = deepcopy(record); damaged["reviewed_at"] = NOW
        damaged["record_hash"] = digest({k: v for k, v in damaged.items() if k != "record_hash"})
        with self.assertRaisesRegex(SafetyError, "REVIEW_BEFORE_MODEL_RESULT"): _verified_definition(damaged)

    def test_foreign_missing_or_false_same_example_answers_are_refused(self):
        item = definition_review_input(self.memory.snapshot(), self.cids[0], as_of=NOW)["item"]
        payload = {"mode": MODE, "definition_contract": CONTRACT, "items": [item]}
        response, _ = Runner()("B", payload, None)
        for change_answer in (
            lambda value: value["references"].append("invented"),
            lambda value: value["example_decisions"].pop(),
            lambda value: value["example_decisions"][0].update(decision="different"),
        ):
            bad = deepcopy(response["results"][0]); change_answer(bad)
            with self.assertRaises(SafetyError): validate_definition_result(item, bad)
        bad = deepcopy(item); bad["examples"].append(deepcopy(bad["examples"][0]))
        with self.assertRaises(SafetyError): validate_definition_item(bad)

    def test_failed_local_application_recovers_original_answer_with_zero_repeat_model_calls(self):
        import product_radar.functional_definition_review as module
        original = module.accept_functional_definition; runner = Runner()
        with patch.object(module, "accept_functional_definition", side_effect=OSError("Fictional save interruption")):
            first = self.run_review(runner)
        self.assertFalse(first["accepted"])
        self.assertTrue(all(row["reason"] == "FUNCTIONAL_DEFINITION_REVIEW_SAVE_FAILED" for row in first["failures"].values()))
        second = self.run_review(runner)
        self.assertEqual(set(second["accepted"]), set(self.cids)); self.assertEqual(len(runner.calls), 1)

    def test_never_started_original_cannot_launch_a_stale_sibling_hidden_by_a_smaller_call(self):
        import product_radar.research_ai as ai
        state = self.memory.snapshot()
        items = [definition_review_input(state, cid, as_of=NOW)["item"] for cid in self.cids]
        runner = Runner(); write = ai.atomic_json
        def stop_before_intent(path, value):
            if Path(path).name == "intent.json": raise OSError("Fictional stop before model intent")
            return write(path, value)
        with patch.object(ai, "atomic_json", side_effect=stop_before_intent):
            with self.assertRaises(OSError):
                Jobs(self.memory, runner).run("B", {"mode": MODE, "definition_contract": CONTRACT, "items": items})
        self.assertEqual(runner.calls, [])
        before = functional_definition_readiness(self.memory, [self.cids[0]], as_of=NOW)
        state = self.memory.snapshot()
        child, association = next((child, row) for child, row in state["associations"].items()
            if row.get("concept_id") == self.cids[1])
        self.memory.put("associations", child, dict(association, status="deferred"), previous=association)
        after = functional_definition_readiness(self.memory, [self.cids[0]], as_of=NOW,
            skip_concept_bindings=before["bindings"])
        self.assertNotEqual(before["bindings"], after["bindings"])
        result = self.run_review(runner, [self.cids[0]])
        self.assertEqual(result["failures"][self.cids[0]]["reason"], "FUNCTIONAL_DEFINITION_ORIGINAL_BATCH_INPUT_CHANGED")
        self.assertEqual(runner.calls, [])

    def test_saved_capsule_still_validates_after_original_temporary_reply_is_removed(self):
        self.run_review(Runner()); current, row = self.definition()
        base = self.memory.root / "ai_jobs" / row["job_id"]
        (current_attempt(base) / "result.json").unlink()
        self.assertEqual(effective_functional_definition(self.memory.snapshot(), current["concept_id"]), current)
        self.assertEqual(self.run_review()["failures"], {})

    def test_definition_and_membership_cannot_borrow_an_unrelated_or_forged_accepted_reply(self):
        self.run_review(Runner()); _, row = self.definition()
        for mutate in (
            lambda record: record["definition"].update(mechanism="Invented powered mechanism"),
            lambda record: next(iter(record["example_decisions"].values())).update(decision="different"),
            lambda record: next(iter(record["job_proof"][PROOF_KEY]["binding"]["children"].values())).update(semantic_id="unrelated-semantic"),
            lambda record: record["job_proof"].pop(PROOF_KEY),
        ):
            bad = deepcopy(row); mutate(bad)
            from product_radar.functional_types import definition_meaning_hash
            bad["meaning_hash"] = definition_meaning_hash(bad["definition"])
            bad["record_hash"] = digest({k: v for k, v in bad.items() if k != "record_hash"})
            with self.assertRaises(SafetyError): _verified_definition(bad)

    def test_direct_new_mode_acceptance_requires_actual_nested_proof(self):
        prepared = definition_review_input(self.memory.snapshot(), self.cids[0], as_of=NOW)
        runner = Runner(); jid, answer = Jobs(self.memory, runner).run("B", {"mode": MODE,
            "definition_contract": CONTRACT, "items": [prepared["item"]]})
        result = answer["accepted"][self.cids[0]]
        with self.assertRaisesRegex(SafetyError, "ACCEPTANCE_PROOF_REQUIRED"):
            accept_functional_definition(self.memory, self.cids[0], result["definition"],
                basis_refs=[next(iter(prepared["binding"]["children"].values()))["observation_id"]],
                reviewer="unrelated_operator", reviewed_at=NOW, expected_meaning_hash=prepared["binding"]["expected_meaning_hash"],
                job_id=jid)

    def test_original_result_change_inside_commit_refuses_definition(self):
        commit = self.memory.commit; injected = False; runner = Runner()
        def race(eid, changes, *, validate=None):
            nonlocal injected
            if eid.startswith("accept-functional") and not injected:
                injected = True
                jid = changes[0]["value"]["job_id"]
                path = current_attempt(self.memory.root / "ai_jobs" / jid) / "result.json"
                raw = load_json(path); raw["response"]["results"][0]["reason"] = "Different actual accepted answer."
                atomic_json(path, raw)
            return commit(eid, changes, validate=validate)
        with patch.object(self.memory, "commit", side_effect=race): result = self.run_review(runner)
        self.assertNotIn(self.cids[0], result["accepted"])
        self.assertFalse(effective_functional_definition(self.memory.snapshot(), self.cids[0])["accepted"])
        self.assertTrue(any("CHANGED" in row["reason"] for row in result["failures"].values()))

    def test_original_association_change_inside_commit_does_not_apply_stale_proposal(self):
        commit = self.memory.commit; injected = False
        def race(eid, changes, *, validate=None):
            nonlocal injected
            if eid.startswith("accept-functional") and not injected:
                injected = True; state = self.memory.snapshot()
                cid = changes[0]["value"]["concept_id"]
                child, previous = next((child, value) for child, value in state["associations"].items() if value.get("concept_id") == cid)
                commit("definition-association-race", [change("associations", child, dict(previous, status="deferred"), previous)])
            return commit(eid, changes, validate=validate)
        with patch.object(self.memory, "commit", side_effect=race): result = self.run_review(Runner())
        self.assertTrue(result["failures"])
        self.assertTrue(result["accepted"], "Independent type must continue after one stale application.")


if __name__ == "__main__": unittest.main()
