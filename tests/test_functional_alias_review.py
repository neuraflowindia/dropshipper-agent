"""Saved-job alias review fixtures; no network, provider or real model calls."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_functional_aliases as fixture
from product_radar.functional_alias_review import (CONTRACT, MODE, PROMPT_VERSION, REVIEW_KIND,
    current_alias_review, prepare_functional_alias_review, review_functional_aliases,
    semantic_alias_item, validate_alias_item, validate_alias_job_proof, verify_alias_job,
    functional_alias_readiness, match_functional_aliases)
from product_radar.functional_aliases import accept_functional_alias, alias_binding, alias_projection, prepare_alias_input
from product_radar.models import SafetyError, digest, utc_now
from product_radar.research_ai import (B_PROMPT_VERSION, Jobs, b_contract, instructions_for,
    prompt_version, schema_for, validate_results)
from product_radar.storage import atomic_json, load_json

REVIEWED = "2026-10-07T10:00:00+00:00"


class Runner:
    identity = "synthetic-functional-alias-v1"

    def __init__(self, *, decision="same", mutate=None):
        self.calls, self.decision, self.mutate = [], decision, mutate

    def __call__(self, role, payload, folder):
        self.calls.append((role, deepcopy(payload)))
        rows = [{"id": item["id"], "decision": self.decision,
                 "reason": "The accepted narrow uses and mechanisms are equivalent in this synthetic fixture."}
                for item in payload["items"]]
        if self.mutate is not None: self.mutate(rows)
        return {"results": rows}, {"input_tokens": 20, "cached_input_tokens": 0, "output_tokens": 10}


class FunctionalAliasReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.FunctionalAliasTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory = self.fixture.memory
        # The older identity-correction fixture's A/B jobs are placeholders,
        # not inputs to Jobs. Retain them but give its unused binding map the
        # real empty shape; new B jobs below are produced by Jobs itself.
        for jid, row in self.memory.snapshot()["jobs"].items():
            if "binding_keys" not in row:
                self.memory.put("jobs", jid, dict(row, binding_keys={}), previous=row)

    def different_wording(self, cid="bird", *, use="Keep books standing upright"):
        self.fixture.accept_definition(cid, dict(self.fixture.definition, primary_use=use),
                                       reviewed_at="2026-10-06T10:00:00+00:00")

    def prepared(self, members=("bear", "bird")):
        return prepare_functional_alias_review(self.memory, list(members), reviewed_at=REVIEWED)

    def run_review(self, runner=None, groups=None):
        return review_functional_aliases(self.memory, groups or [["bear", "bird"]], runner, reviewed_at=REVIEWED)

    def one_job(self):
        return next((jid, row) for jid, row in self.memory.snapshot()["jobs"].items()
                    if row.get("request", {}).get("prompt_version") == PROMPT_VERSION)

    def test_legacy_B_contract_and_new_mode_are_separate(self):
        old = {"items": [{"id": "child", "candidates": [{"id": "bear"}]}]}
        self.assertEqual(prompt_version("B", old), B_PROMPT_VERSION)
        self.assertIn("match_id", schema_for("B", old)["properties"]["results"]["items"]["properties"])
        accepted = validate_results("B", old, {"results": [{"id": "child", "decision": "same",
            "match_id": "bear", "reason": "Legacy candidate."}]})
        self.assertIn("child", accepted["accepted"])
        payload = self.prepared()["payload"]
        self.assertEqual(prompt_version("B", payload), PROMPT_VERSION)
        self.assertNotIn("match_id", schema_for("B", payload)["properties"]["results"]["items"]["properties"])
        self.assertIn("compatibility", instructions_for("B", payload))
        with self.assertRaises(SafetyError): b_contract(payload, B_PROMPT_VERSION)
        with self.assertRaises(SafetyError): b_contract(old, PROMPT_VERSION)
        with self.assertRaises(SafetyError): b_contract(dict(payload, alias_contract="unknown"))

    def test_exact_meaning_uses_zero_model_calls_and_no_alias_write(self):
        runner = Runner(); before = self.memory.snapshot()
        result = self.run_review(runner)
        row = next(iter(result["accepted"].values()))
        self.assertEqual(row["method"], "exact_meaning")
        self.assertEqual(row["decision"], "same")
        self.assertEqual(runner.calls, [])
        self.assertEqual(before, self.memory.snapshot())
        self.assertIsNone(alias_binding(self.memory.snapshot()))

    def test_real_saved_job_and_review_replay_make_one_call(self):
        self.different_wording(); runner = Runner()
        first = self.run_review(runner); after = self.memory.snapshot()
        self.assertFalse(first["failures"])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(first, self.run_review(runner))
        self.assertEqual(after, self.memory.snapshot())
        self.assertIsNone(alias_binding(after))
        row = next(iter(first["accepted"].values()))
        self.assertEqual(row["method"], "model")
        self.assertEqual(row["job_proof"]["original_job"], after["jobs"][row["job_proof"]["job_id"]])
        self.assertTrue(any(r.get("kind") == REVIEW_KIND for r in after["source_checks"].values()))

    def test_title_and_review_clock_changes_reuse_same_semantic_job(self):
        self.different_wording(); runner = Runner(); first = self.run_review(runner)
        old_item = self.prepared()["item"]
        definition = self.fixture.definition | {"primary_use": "Keep books standing upright", "report_title": "New display wording"}
        self.fixture.accept_definition("bird", definition, reviewed_at="2026-10-08T10:00:00+00:00")
        new = prepare_functional_alias_review(self.memory, ["bird", "bear"], reviewed_at="2026-10-08T11:00:00+00:00")
        self.assertEqual(new["item"], old_item)
        second = review_functional_aliases(self.memory, [["bear", "bird"]], runner,
                                           reviewed_at="2026-10-08T11:00:00+00:00")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(next(iter(first["accepted"].values()))["record_id"],
                         next(iter(second["accepted"].values()))["record_id"])
        jid, _ = self.one_job()
        proof = verify_alias_job(self.memory, self.memory.snapshot(), new["input"], jid, new["item"]["id"])
        self.assertEqual(proof["decision"], "same")

    def test_material_meaning_change_needs_different_item_not_cached_answer(self):
        self.different_wording(); runner = Runner(); self.run_review(runner)
        old = self.prepared()["item"]
        self.fixture.accept_definition("bird", dict(self.fixture.definition, mechanism="Powered book clamp"),
                                       reviewed_at="2026-10-06T11:00:00+00:00")
        new = self.prepared()
        self.assertNotEqual(new["item"]["id"], old["id"])
        self.assertIsNone(current_alias_review(self.memory.snapshot(), new["input"]))
        self.run_review(runner)
        self.assertEqual(len(runner.calls), 2)

    def test_literal_boundaries_and_hashes_cannot_be_invented(self):
        prepared = self.prepared(); item = prepared["item"]
        forged = deepcopy(item); forged["members"][0]["definition"]["excluded_distinctions"] = []
        with self.assertRaises(SafetyError): validate_alias_item(forged)
        forged = deepcopy(item); forged["members"][0]["definition"]["report_title"] = "Not a semantic field"
        with self.assertRaises(SafetyError): validate_alias_item(forged)
        prepared["input"]["definition_bindings"]["bird"]["record_hash"] = "0" * 64
        prepared["input"]["input_hash"] = digest({k: v for k, v in prepared["input"].items() if k != "input_hash"})
        with self.assertRaisesRegex(SafetyError, "DEFINITION_CHANGED"):
            semantic_alias_item(self.memory.snapshot(), prepared["input"])

    def test_unknown_original_job_does_not_call_model_again(self):
        self.different_wording()
        class Unknown(Runner):
            def __call__(self, role, payload, folder):
                self.calls.append(role)
                raise SafetyError("SYNTHETIC_EXECUTION_UNKNOWN")
        runner = Unknown()
        first = self.run_review(runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertFalse(first["accepted"])
        second = self.run_review(runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(set(second["failures"].values()), {"AI_JOB_PENDING_RECONCILIATION"})

    def test_bad_sibling_does_not_drop_valid_saved_judgment(self):
        self.different_wording(); self.different_wording("cat", use="Support books in a standing position")
        def bad(rows): rows[0]["match_id"] = "bear"
        runner = Runner(mutate=bad)
        result = self.run_review(runner, [["bear", "bird"], ["bear", "cat"]])
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(set(result["failures"].values()), {"AI_FIELDS_INVALID"})
        self.assertEqual(len(runner.calls), 1)

    def test_local_application_failure_recovers_original_without_second_model_call(self):
        self.different_wording(); self.different_wording("cat", use="Support books in a standing position")
        import product_radar.functional_alias_review as module
        original = module._save_review
        def fail_one(memory, prepared, item, jid):
            if "bird" in prepared["member_ids"]: raise OSError("Synthetic local save failure")
            return original(memory, prepared, item, jid)
        runner = Runner()
        with patch.object(module, "_save_review", side_effect=fail_one):
            first = self.run_review(runner, [["bear", "bird"], ["bear", "cat"]])
        self.assertEqual(len(first["accepted"]), 1)
        self.assertEqual(set(first["failures"].values()), {"FUNCTIONAL_ALIAS_REVIEW_SAVE_FAILED"})
        second = self.run_review(runner, [["bear", "bird"], ["bear", "cat"]])
        self.assertEqual(len(second["accepted"]), 2)
        self.assertFalse(second["failures"])
        self.assertEqual(len(runner.calls), 1)

    def test_derived_aggregate_id_cannot_authenticate_an_alias(self):
        self.different_wording(); self.different_wording("cat", use="Support books in a standing position")
        runner = Runner(); bear_bird = self.prepared(); bear_cat = self.prepared(("bear", "cat"))
        first_id, _ = Jobs(self.memory, runner).run("B", bear_bird["payload"])
        payload = dict(bear_bird["payload"], items=[bear_bird["item"], bear_cat["item"]])
        derived_id, _ = Jobs(self.memory, runner).run("B", payload)
        self.assertNotIn(derived_id, self.memory.snapshot()["jobs"])
        with self.assertRaisesRegex(SafetyError, "JOB_INVALID"):
            verify_alias_job(self.memory, self.memory.snapshot(), bear_bird["input"], derived_id, bear_bird["item"]["id"])
        checked = verify_alias_job(self.memory, self.memory.snapshot(), bear_bird["input"], first_id, bear_bird["item"]["id"])
        self.assertEqual(checked["decision"], "same")

    def test_proof_requires_original_job_request_and_actual_accepted_result(self):
        self.different_wording(); self.run_review(Runner())
        prepared = self.prepared(); jid, _ = self.one_job()
        verified = verify_alias_job(self.memory, self.memory.snapshot(), prepared["input"], jid, prepared["item"]["id"])
        proof = verified["proof"]
        self.assertEqual(validate_alias_job_proof(self.memory.snapshot(), prepared["input"], proof)["decision"], "same")
        bad = deepcopy(proof); bad["accepted_item"]["decision"] = "different"; bad["accepted_item_hash"] = digest(bad["accepted_item"])
        with self.assertRaisesRegex(SafetyError, "NOT_ACCEPTED"):
            validate_alias_job_proof(self.memory.snapshot(), prepared["input"], bad)
        state = self.memory.snapshot(); state["jobs"][jid]["request"]["runner"] = "another_runner"
        with self.assertRaisesRegex(SafetyError, "JOB_INVALID"):
            validate_alias_job_proof(state, prepared["input"], proof)

    def test_actual_result_missing_or_changed_cannot_be_initially_verified(self):
        self.different_wording(); self.run_review(Runner()); prepared = self.prepared(); jid, _ = self.one_job()
        verified = verify_alias_job(self.memory, self.memory.snapshot(), prepared["input"], jid, prepared["item"]["id"])
        path = verified["result_path"]; receipt = load_json(path)
        receipt["request_hash"] = "0" * 64; atomic_json(path, receipt)
        with self.assertRaisesRegex(SafetyError, "RESULT_CHANGED"):
            verify_alias_job(self.memory, self.memory.snapshot(), prepared["input"], jid, prepared["item"]["id"])
        path.unlink()
        with self.assertRaises(SafetyError):
            verify_alias_job(self.memory, self.memory.snapshot(), prepared["input"], jid, prepared["item"]["id"])

    def test_definition_change_during_save_refuses_stale_application(self):
        self.different_wording(); runner = Runner(); commit = self.memory.commit; injected = False
        def race(eid, changes, *, validate=None):
            nonlocal injected
            if not injected and eid.startswith("save-functional-alias-review"):
                injected = True
                self.fixture.accept_definition("bird", dict(self.fixture.definition, mechanism="Powered mechanism"),
                    reviewed_at="2026-10-06T12:00:00+00:00")
            return commit(eid, changes, validate=validate)
        with patch.object(self.memory, "commit", side_effect=race): result = self.run_review(runner)
        self.assertFalse(result["accepted"])
        self.assertEqual(set(result["failures"].values()), {"FUNCTIONAL_ALIAS_INPUT_CHANGED"})
        self.assertEqual(len(runner.calls), 1)
        self.assertFalse(any(row.get("kind") == REVIEW_KIND for row in self.memory.snapshot()["source_checks"].values()))

    def test_original_result_change_during_commit_refuses_review(self):
        self.different_wording(); runner = Runner(); commit = self.memory.commit; injected = False
        def race(eid, changes, *, validate=None):
            nonlocal injected
            if not injected and eid.startswith("save-functional-alias-review"):
                injected = True
                jid, _ = self.one_job()
                path = self.memory.root / "ai_jobs" / jid / "result.json"
                receipt = load_json(path); receipt["usage"]["output_tokens"] += 1; atomic_json(path, receipt)
            return commit(eid, changes, validate=validate)
        with patch.object(self.memory, "commit", side_effect=race): result = self.run_review(runner)
        self.assertFalse(result["accepted"])
        self.assertEqual(set(result["failures"].values()), {"FUNCTIONAL_ALIAS_JOB_RESULT_CHANGED"})

    def test_different_and_unclear_are_cached_reviews_not_aliases(self):
        self.different_wording()
        for decision in ("different", "unclear"):
            with self.subTest(decision=decision):
                runner = Runner(decision=decision)
                # A different member group is a new bounded review, never a
                # hidden retry to fish for a preferred same answer.
                group = ["bear", "bird"] if decision == "different" else ["bird", "cat"]
                first = self.run_review(runner, [group]); self.run_review(runner, [group])
                self.assertEqual(next(iter(first["accepted"].values()))["decision"], decision)
                self.assertEqual(len(runner.calls), 1)
        self.assertIsNone(alias_binding(self.memory.snapshot()))

    def test_matching_exact_group_applies_existing_authority_with_zero_model_calls(self):
        runner = Runner(); before = self.memory.snapshot()
        ready = functional_alias_readiness(self.memory, before, ["bird"], as_of=REVIEWED)
        self.assertTrue(ready["local_ready"])
        result = match_functional_aliases(self.memory, ["bird"], runner, reviewed_at=REVIEWED)
        self.assertTrue(result["outcomes"][0]["applied"])
        self.assertEqual(runner.calls, [])
        after = self.memory.snapshot()
        self.assertEqual(alias_projection(after)["bird"]["member_ids"], ["bear", "bird", "cat"])
        self.assertEqual(before["concepts"], after["concepts"])
        self.assertEqual(before["observations"], after["observations"])
        self.assertFalse(functional_alias_readiness(self.memory, after, ["bird"], as_of=REVIEWED)["ready"])

    def test_matching_retrieves_bounded_same_category_and_applies_actual_job(self):
        self.different_wording(); runner = Runner()
        result = match_functional_aliases(self.memory, ["bird"], runner, reviewed_at=REVIEWED, limit=1)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(runner.calls[0][1]["items"]), 1)
        self.assertTrue(result["outcomes"][0]["applied"])
        self.assertEqual(result["outcomes"][0]["decision"], "same")
        self.assertEqual(alias_projection(self.memory.snapshot())["bird"]["member_ids"], ["bear", "bird"])

    def test_matching_unchanged_unclear_is_not_repeated(self):
        self.different_wording(); runner = Runner(decision="unclear")
        first = match_functional_aliases(self.memory, ["bird"], runner, reviewed_at=REVIEWED, limit=2)
        self.assertEqual({row["decision"] for row in first["outcomes"]}, {"unclear"})
        second = match_functional_aliases(self.memory, ["bird"], runner, reviewed_at=REVIEWED, limit=2)
        self.assertEqual(second["outcomes"], [])
        self.assertEqual(len(runner.calls), 1)
        self.assertFalse(functional_alias_readiness(self.memory, self.memory.snapshot(), ["bird"],
            as_of=REVIEWED, runner_available=True)["ready"])

    def test_matching_no_function_or_mechanism_overlap_does_not_send_whole_history(self):
        self.fixture.accept_definition("other", {**self.fixture.definition,
            "primary_use": "Measure air pressure", "mechanism": "Electronic pressure sensor", "use_context": ["Bicycle wheel"]},
            reviewed_at="2026-10-06T10:00:00+00:00")
        runner = Runner()
        result = match_functional_aliases(self.memory, ["other"], runner, reviewed_at=REVIEWED)
        self.assertEqual(result["outcomes"], [])
        self.assertEqual(runner.calls, [])

    def test_matching_queue_conflict_names_exact_issues_and_stable_binding(self):
        self.memory.put("issue_reservations", "one", {"state": "uploaded", "products": [{"concept_id": "bear", "status": "New"}]})
        self.memory.put("issue_reservations", "two", {"state": "uploaded", "products": [{"concept_id": "bird", "status": "New"}]})
        hint = functional_alias_readiness(self.memory, self.memory.snapshot(), ["bird"], as_of=REVIEWED)
        later = functional_alias_readiness(self.memory, self.memory.snapshot(), ["bird"], as_of="2026-10-08T10:00:00+00:00")
        self.assertEqual(hint["signal"], later["signal"])
        result = match_functional_aliases(self.memory, ["bird"], Runner(), reviewed_at=REVIEWED)
        self.assertEqual(result["outcomes"][0]["decision"], "release_review_required")
        self.assertEqual(result["outcomes"][0]["affected_issue_ids"], ["one", "two"])
        self.assertIsNone(alias_binding(self.memory.snapshot()))
        skipped = functional_alias_readiness(self.memory, self.memory.snapshot(), ["bird"], as_of=REVIEWED,
            skip_group_ids=[hint["groups"][0]["group_id"]])
        self.assertFalse(skipped["ready"])
        previous = self.memory.snapshot()["issue_reservations"]["two"]
        self.memory.put("issue_reservations", "two", dict(previous, state="withdrawn"), previous=previous)
        changed = functional_alias_readiness(self.memory, self.memory.snapshot(), ["bird"], as_of=REVIEWED)
        self.assertNotEqual(hint["groups"][0]["input_hash"], changed["groups"][0]["input_hash"])
        self.assertTrue(match_functional_aliases(self.memory, ["bird"], reviewed_at=REVIEWED)["outcomes"][0]["applied"])

    def test_original_reply_arrival_wakes_without_new_state_and_uses_zero_model_calls(self):
        self.different_wording(); prepared = self.prepared()
        class Unknown(Runner):
            def __call__(self, role, payload, folder):
                self.calls.append(role); raise SafetyError("SYNTHETIC_EXECUTION_UNKNOWN")
        unknown = Unknown(); self.run_review(unknown)
        state = self.memory.snapshot(); jid, job = self.one_job()
        pending = functional_alias_readiness(self.memory, state, ["bird"], as_of=REVIEWED, limit=1,
                                            runner_available=True)
        self.assertFalse(pending["ready"])
        atomic_json(self.memory.root / "ai_jobs" / jid / "result.json", {"request_hash": digest(job["request"]),
            "response": {"results": [{"id": prepared["item"]["id"], "decision": "same", "reason": "Late original saved answer."}]},
            "usage": {}, "completed_at": utc_now()})
        ready = functional_alias_readiness(self.memory, state, ["bird"], as_of=REVIEWED, limit=1)
        self.assertTrue(ready["local_ready"])
        self.assertNotEqual(pending["signal"], ready["signal"])
        result = match_functional_aliases(self.memory, ["bird"], None, reviewed_at=REVIEWED, limit=1)
        self.assertTrue(result["outcomes"][0]["applied"])
        self.assertEqual(len(unknown.calls), 1)

    def test_initial_alias_acceptance_rechecks_actual_file_not_rehashed_supplied_proof(self):
        self.different_wording(); self.run_review(Runner()); prepared = self.prepared()
        jid, _ = self.one_job()
        original = verify_alias_job(self.memory, self.memory.snapshot(), prepared["input"], jid, prepared["item"]["id"])
        bad = deepcopy(original["proof"])
        bad["accepted_item"]["reason"] = "Invented caller decision."
        bad["accepted_item_hash"] = digest(bad["accepted_item"])
        bad["result_receipt"]["response"]["results"][0]["reason"] = bad["accepted_item"]["reason"]
        bad["result_hash"] = digest(bad["result_receipt"])
        # The portable capsule is internally consistent but not the original
        # file. New acceptance must verify that file before trusting it.
        validate_alias_job_proof(self.memory.snapshot(), prepared["input"], bad)
        with self.assertRaisesRegex(SafetyError, "ORIGINAL_JOB_MISMATCH"):
            accept_functional_alias(self.memory, prepared["input"], method="model", reviewer="synthetic",
                reason=bad["accepted_item"]["reason"], job_proof=bad, job_id=jid, item_id=prepared["item"]["id"])
        self.assertIsNone(alias_binding(self.memory.snapshot()))

    def test_programme_match_join_is_work_turn_only_and_preserves_source_result(self):
        from product_radar import programme
        from product_radar.work_turns import bind_work_turn, close_work_turn, open_work_turn
        record = {"programme_id": "synthetic-alias-programme", "concept_ids": ["bird"],
                  "source_run_id": "synthetic-sources", "deferrals": {}}
        context = {"runner": None, "at": REVIEWED, "match_limit": 5, "retry_seconds": 300}
        empty = {"outcomes": [], "reused": []}
        with patch("product_radar.source_interpretation._questions", return_value=[]), \
             patch("product_radar.source_interpretation.review_source_results", return_value=empty):
            self.assertEqual(programme._match(self.memory, record, context), empty)
            self.assertIsNone(alias_binding(self.memory.snapshot()))
            turn = open_work_turn(self.memory, record["programme_id"], at=REVIEWED)
            try:
                with bind_work_turn(self.memory, turn["id"]):
                    joined = programme._match(self.memory, record, context)
            finally:
                close_work_turn(self.memory, turn["id"], at=REVIEWED, reason="Synthetic alias match completed")
        self.assertEqual(joined["outcomes"], [])
        self.assertTrue(joined["functional_aliases"]["outcomes"][0]["applied"])

    def test_programme_alias_conflict_wait_does_not_block_source_matching(self):
        from product_radar import programme
        from product_radar.work_turns import bind_work_turn, close_work_turn, open_work_turn
        self.memory.put("issue_reservations", "two", {"state": "uploaded", "products": [
            {"concept_id": "bear", "status": "New"}, {"concept_id": "bird", "status": "New"}]})
        record = {"programme_id": "synthetic-alias-programme", "concept_ids": ["bird"],
                  "source_run_id": "synthetic-sources", "deferrals": {}}
        context = {"runner": None, "at": REVIEWED, "match_limit": 5, "retry_seconds": 300}
        turn = open_work_turn(self.memory, record["programme_id"], at=REVIEWED)
        try:
            with bind_work_turn(self.memory, turn["id"]), \
                 patch("product_radar.source_interpretation._questions", return_value=[]), \
                 patch("product_radar.source_interpretation.review_source_results", return_value={"outcomes": [], "reused": []}) as source:
                first = programme._match(self.memory, record, context)
                second = programme._match(self.memory, record, context)
        finally:
            close_work_turn(self.memory, turn["id"], at=REVIEWED, reason="Synthetic conflict waits only affected aliases")
        self.assertEqual(source.call_count, 2)
        self.assertEqual(first["functional_aliases"]["outcomes"][0]["affected_issue_ids"], ["two"])
        self.assertEqual(second["functional_aliases"]["outcomes"], [])
        self.assertEqual({row["stage"] for row in record["deferrals"].values()},
                         {"functional_alias", "functional_example"})
        example_wait = next(row for row in record["deferrals"].values() if row["stage"] == "functional_example")
        self.assertEqual(example_wait["reason"], "AI_RUNNER_NOT_ENABLED")

    def test_absent_runner_and_unstarted_original_do_not_create_fake_intent(self):
        self.different_wording(); prepared = self.prepared(); runner = Runner()
        import product_radar.research_ai as module
        original = module.atomic_json
        def fail_intent(path, value):
            if path.name == "intent.json": raise OSError("Synthetic pre-intent save failed")
            return original(path, value)
        with patch.object(module, "atomic_json", side_effect=fail_intent):
            with self.assertRaises(OSError): Jobs(self.memory, runner).run("B", prepared["payload"])
        jid, _ = self.one_job(); before = self.memory.snapshot()
        result = self.run_review(None)
        self.assertEqual(set(result["failures"].values()), {"AI_RUNNER_NOT_ENABLED"})
        self.assertEqual(before, self.memory.snapshot())
        self.assertFalse((self.memory.root / "ai_jobs" / jid / "intent.json").exists())
        self.assertEqual(runner.calls, [])

    def test_prepared_original_and_novel_sibling_allow_only_one_new_model_batch(self):
        self.different_wording(); self.different_wording("cat", use="Support books in a standing position")
        prepared = self.prepared(); runner = Runner()
        import product_radar.research_ai as module
        original = module.atomic_json
        def fail_intent(path, value):
            if path.name == "intent.json": raise OSError("Synthetic pre-intent save failed")
            return original(path, value)
        with patch.object(module, "atomic_json", side_effect=fail_intent):
            with self.assertRaises(OSError): Jobs(self.memory, runner).run("B", prepared["payload"])
        result = self.run_review(runner, [["bear", "bird"], ["bear", "cat"]])
        self.assertEqual(len(result["accepted"]), 1)
        self.assertEqual(set(result["failures"].values()), {"FUNCTIONAL_ALIAS_REVIEW_BATCH_LIMIT"})
        self.assertEqual(len(runner.calls), 1)
        current = self.run_review(runner, [["bear", "bird"], ["bear", "cat"]])
        self.assertEqual(len(current["accepted"]), 2)
        self.assertEqual(len(runner.calls), 2)

    def test_literal_payload_budget_defers_extra_groups_without_an_extra_batch(self):
        # Four actual accepted definitions, six pair questions. Each literal
        # definition is below its32KiB cap; all six pairs together exceed the
        # existing256KiB Jobs payload cap and must not start a second batch.
        details = ["Boundary " + str(n) + " " + "x" * 1850 for n in range(12)]
        for index, cid in enumerate(("bear", "bird", "cat", "other")):
            self.fixture.accept_definition(cid, dict(self.fixture.definition,
                primary_use="Hold books upright with fixture distinction " + str(index), included_distinctions=details),
                reviewed_at="2026-10-06T12:00:00+00:00")
        groups = [["bear", "bird"], ["bear", "cat"], ["bear", "other"],
                  ["bird", "cat"], ["bird", "other"], ["cat", "other"]]
        runner = Runner(decision="unclear")
        first = self.run_review(runner, groups)
        self.assertEqual(len(runner.calls), 1)
        self.assertTrue(first["accepted"])
        self.assertEqual(set(first["failures"].values()), {"FUNCTIONAL_ALIAS_REVIEW_BATCH_LIMIT"})
        second = self.run_review(runner, groups)
        self.assertFalse(second["failures"])
        self.assertEqual(len(second["accepted"]), 6)
        self.assertEqual(len(runner.calls), 2)

    def test_all_matching_deferrals_are_filtered_before_bounded_page_is_filled(self):
        for index, cid in enumerate(("bear", "bird", "cat", "other")):
            self.fixture.accept_definition(cid, dict(self.fixture.definition,
                primary_use="Hold books upright with fixture distinction " + str(index)),
                reviewed_at="2026-10-06T12:00:00+00:00")
        state = self.memory.snapshot(); targets = ["bear", "bird", "cat", "other"]
        all_groups = functional_alias_readiness(self.memory, state, targets, as_of=REVIEWED,
                                               runner_available=True, limit=10)
        self.assertEqual(len(all_groups["groups"]), 6)
        waiting = {g["group_id"]: g["input_hash"] for g in all_groups["groups"]}
        page = functional_alias_readiness(self.memory, state, targets, as_of=REVIEWED, limit=2,
                                         runner_available=True, skip_group_bindings=waiting)
        self.assertFalse(page["ready"])
        self.assertEqual(page["groups"], [])
        runner = Runner()
        result = match_functional_aliases(self.memory, targets, runner, reviewed_at=REVIEWED, limit=2,
                                         skip_group_bindings=waiting)
        self.assertEqual(result["outcomes"], [])
        self.assertEqual(runner.calls, [])

    def test_late_original_breaks_only_its_saved_binding_wait(self):
        self.different_wording(); prepared = self.prepared()
        class Unknown(Runner):
            def __call__(self, role, payload, folder):
                self.calls.append(role); raise SafetyError("SYNTHETIC_EXECUTION_UNKNOWN")
        self.run_review(Unknown()); state = self.memory.snapshot(); jid, job = self.one_job()
        pending = functional_alias_readiness(self.memory, state, ["bird"], as_of=REVIEWED, limit=1)
        waits = {g["group_id"]: g["input_hash"] for g in pending["groups"]}
        self.assertFalse(functional_alias_readiness(self.memory, state, ["bird"], as_of=REVIEWED, limit=1,
            skip_group_bindings=waits)["ready"])
        atomic_json(self.memory.root / "ai_jobs" / jid / "result.json", {"request_hash": digest(job["request"]),
            "response": {"results": [{"id": prepared["item"]["id"], "decision": "same", "reason": "Late original saved answer."}]},
            "usage": {}, "completed_at": utc_now()})
        ready = functional_alias_readiness(self.memory, state, ["bird"], as_of=REVIEWED, limit=1,
                                          skip_group_bindings=waits)
        self.assertTrue(ready["local_ready"])
        self.assertNotEqual(ready["groups"][0]["input_hash"], waits[prepared["item"]["id"]])

    def test_unchanged_bad_original_receipt_obeys_its_recorded_binding_wait(self):
        self.different_wording(); prepared = self.prepared(); runner = Runner()
        with patch("product_radar.functional_alias_review._save_review", side_effect=OSError("Synthetic local save failed")):
            self.run_review(runner)
        jid, _ = self.one_job()
        # The actual original result exists but local derived application never
        # finished. Corrupt that original before recovery; its unchanged stat
        # must not create repeated empty probes under the same saved wait.
        path = self.memory.root / "ai_jobs" / jid / "result.json"
        receipt = load_json(path); receipt["request_hash"] = "0" * 64; atomic_json(path, receipt)
        result = match_functional_aliases(self.memory, ["bird"], None, reviewed_at=REVIEWED, limit=1)
        self.assertEqual(result["outcomes"][0]["decision"], "waiting")
        waits = {g["group_id"]: g["input_hash"] for g in result["outcomes"]}
        hint = functional_alias_readiness(self.memory, self.memory.snapshot(), ["bird"], as_of=REVIEWED,
            limit=1, skip_group_bindings=waits)
        self.assertFalse(hint["ready"])
        self.assertEqual(len(runner.calls), 1)

    def test_real_negative_review_prevents_later_transitive_positive_union(self):
        self.different_wording(); self.different_wording("cat", use="Support books in a standing position")
        negative = self.run_review(Runner(decision="different"), [["bear", "cat"]])
        negative_id = next(iter(negative["accepted"].values()))["record_id"]
        before = self.memory.snapshot()["observations"]
        first = match_functional_aliases(self.memory, ["bird"], Runner(), reviewed_at=REVIEWED, limit=1)
        self.assertTrue(first["outcomes"][0]["applied"])
        blocked = match_functional_aliases(self.memory, ["cat"], Runner(), reviewed_at=REVIEWED, limit=1)
        self.assertEqual(blocked["outcomes"][0]["decision"], "waiting")
        self.assertEqual(blocked["outcomes"][0]["reason"], "FUNCTIONAL_ALIAS_CONTRADICTORY_REVIEW")
        self.assertEqual(blocked["outcomes"][0]["review_ids"], [negative_id])
        projection = alias_projection(self.memory.snapshot())
        self.assertEqual(projection["cat"]["member_ids"], ["cat"])
        self.assertEqual(projection["bird"]["member_ids"], ["bear", "bird"])
        self.assertEqual(self.memory.snapshot()["observations"], before)

    def test_current_negative_review_changes_only_the_affected_application_binding(self):
        self.different_wording(); self.different_wording("cat", use="Support books in a standing position")
        match_functional_aliases(self.memory, ["bird"], Runner(), reviewed_at=REVIEWED, limit=1)
        before = functional_alias_readiness(self.memory, self.memory.snapshot(), ["cat"], as_of=REVIEWED, limit=1,
                                            runner_available=True)
        self.run_review(Runner(decision="different"), [["bear", "cat"]])
        after = functional_alias_readiness(self.memory, self.memory.snapshot(), ["cat"], as_of=REVIEWED, limit=1,
                                           runner_available=True)
        # The already-known negative pair settles and is skipped. The next
        # narrow candidate includes its exact negative-component dependency.
        self.assertNotEqual(before["signal"], after["signal"])
        blocked = match_functional_aliases(self.memory, ["cat"], Runner(), reviewed_at=REVIEWED, limit=1)
        self.assertEqual(blocked["outcomes"][0]["reason"], "FUNCTIONAL_ALIAS_CONTRADICTORY_REVIEW")
        waits = {g["group_id"]: g["input_hash"] for g in blocked["outcomes"]}
        self.assertFalse(functional_alias_readiness(self.memory, self.memory.snapshot(), ["cat"], as_of=REVIEWED,
            limit=1, skip_group_bindings=waits)["ready"])
        # That judgment is tied to the actual old meaning; a genuinely revised
        # accepted meaning must be reviewed on its own, not inherit its failure.
        self.fixture.accept_definition("cat", dict(self.fixture.definition, primary_use="Keep books standing upright"),
            reviewed_at="2026-10-06T13:00:00+00:00")
        changed = match_functional_aliases(self.memory, ["cat"], reviewed_at=REVIEWED, limit=1)
        self.assertTrue(changed["outcomes"][0]["applied"])


if __name__ == "__main__": unittest.main()
