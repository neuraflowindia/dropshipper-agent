"""Saved publication -> comparable change -> bounded synthetic E -> allocation.

No source, provider, cloud or client-publication calls are performed. Release
receipts below are explicitly synthetic inputs to the real local writer.
"""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar.issue_allocation import (allocate_issues, allocation_snapshot_hash, reserve_issue,
    record_publication_receipt)
from product_radar.material_updates import (BASELINE_CONTRACT, REVIEW_KIND, SCOPED_VERSION,
    UPDATE_PROMPT_VERSION, comparable_changes, current_material_update, find_material_changes,
    latest_published_baseline, latest_published_baseline_for_members, material_snapshot, prepare_material_update,
    review_material_update, run_material_updates, update_baseline_current, update_item, update_payload, validate_update_result)
from product_radar.models import SafetyError, digest
from product_radar.report_selection import build_report_pool
from product_radar.research_ai import Jobs, prompt_version
from product_radar.research_memory import ResearchMemory, change
from product_radar.storage import Journal
from product_radar.work_turns import DEFAULT_POLICY, bind_work_turn, close_work_turn, open_work_turn
from test_report_selection import fixture, NOW
from test_fit_pipeline import older_format_scores


class Runner:
    identity = "synthetic-material-update-runner"

    def __init__(self, decision="useful_update", bad=False, uncertain=False):
        self.calls, self.decision, self.bad, self.uncertain = 0, decision, bad, uncertain

    def __call__(self, role, payload, folder):
        self.calls += 1
        if self.uncertain:
            raise TimeoutError("Synthetic lost original reply")
        assert role == "E" and payload["mode"] == "material_update_review"
        return {"results": [{"id": row["id"], "decision": self.decision,
            "reason": "The matching stated purchase conditions now give a lower example price.",
            "practical_consequence": "Recheck this lower quoted cost before choosing a supplier." if self.decision == "useful_update" else "",
            "changed_fields": ["prices"], "references": ["invented"] if self.bad else ["old:prices", "new:prices"]}
            for row in payload["items"]]}, {"synthetic": True}


def saved_fixture(root, count=10):
    memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
    state = fixture(count).snapshot()
    memory.commit("synthetic-research", [change(table, rid, row) for table, rows in state.items() for rid, row in rows.items()])
    return memory


def published(memory, *, concept_ids=None):
    pool = build_report_pool(memory, as_of=NOW)
    eligible = [row for row in pool["eligible"] if concept_ids is None or row["concept_id"] in concept_ids]
    issue = allocate_issues(eligible, [{"issue_id": "original", "date": "2026-10-06", "client_id": "default"}], {}, {})["issues"][0]
    reserve_issue(memory, issue, expected_input_hash=issue["input_hash"],
        expected_snapshot_hash=allocation_snapshot_hash(memory.snapshot()), as_of=NOW)
    receipt = {"issue_id": issue["issue_id"], "revision": issue["revision"], "selection_hash": issue["selection_hash"],
        "receipt_id": "synthetic-receipt", "verified": True, "authority": "release_index", "remote_state": "active",
        "package_hash": "a" * 64, "facts_hash": "b" * 64, "server_activated_at": "2026-10-06T08:00:00+00:00",
        "scheduled_at": "2026-10-06T09:00:00+00:00", "server_observed_at": NOW, "server_deactivated_at": None}
    record_publication_receipt(memory, issue["issue_id"], receipt, as_of=NOW)
    return issue, receipt


def reduce_price(memory, n=0, amount=100):
    state = memory.snapshot()
    original_id = f"amazon{n:03d}"
    row = deepcopy(state["observations"][original_id])
    # Actual originals are immutable. A later collection creates a new head.
    step = 1 + sum(oid.startswith(original_id + "-price-") for oid in state["observations"])
    rid = original_id + f"-price-{step}"
    row["observed_at"] = f"2026-10-06T09:{step:02d}:00+00:00"
    row["acquisition_id"] = rid + "-capture"
    row["facts"]["price"]["displayed"] = amount
    review = deepcopy(state["market_reviews"][f"review{n}"])
    review.update(reviewed_at=NOW, input_hash=digest(rid),
        relevance=[{"id": rid, "relationship": "exact_product", "reason": "Synthetic matching later listing."}])
    memory.commit("synthetic-price-" + rid, [change("observations", rid, row), change("market_reviews", rid + "-review", review)])


def question(memory, n=0):
    pool = build_report_pool(memory, as_of=NOW)
    return next(row for row in pool["needed_material_update_work"] if row["binding"]["concept_id"] == f"c{n:03d}")


@older_format_scores
class MaterialUpdateReviewTests(unittest.TestCase):
    def setUp(self):
        clock = patch("product_radar.research_ai.utc_now", return_value=NOW)
        clock.start(); self.addCleanup(clock.stop)
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = saved_fixture(self.root)
        self.issue, self.receipt = published(self.memory)

    def test_actual_publication_writer_freezes_exact_package_and_facts(self):
        state = self.memory.snapshot()
        original = next(iter(state["published_history"].values()))["publication_baseline"]
        self.assertEqual(original["contract"], BASELINE_CONTRACT)
        self.assertEqual(original["package_hash"], self.receipt["package_hash"])
        self.assertEqual(original["package_facts_hash"], self.receipt["facts_hash"])
        self.assertEqual(original["material_facts_hash"], digest(original["snapshot"]))
        before = self.memory.snapshot()
        record_publication_receipt(self.memory, "original", self.receipt, as_of=NOW)
        self.assertEqual(self.memory.snapshot(), before)

    def test_bad_published_capsule_is_named_wait_not_whole_pool_crash(self):
        state = self.memory.snapshot()
        rid, old = next((ref, row) for ref, row in state["published_history"].items() if row["concept_id"] == "c000")
        row = deepcopy(old); row["publication_baseline"]["snapshot"] = []
        row["publication_baseline"]["binding_hash"] = digest({k: v for k, v in row["publication_baseline"].items() if k != "binding_hash"})
        self.memory.put("published_history", rid, row, previous=old)
        pool = build_report_pool(self.memory, as_of=NOW)
        decision = next(row for row in pool["decisions"] if row["concept_id"] == "c000")
        self.assertEqual(decision["useful_update_decision"], "baseline_invalid")
        self.assertEqual(len(pool["decisions"]), 10)

    def test_changed_supplied_snapshot_cannot_keep_old_actual_binding(self):
        reduce_price(self.memory); prepared = question(self.memory)
        # An internally inconsistent item must be rejected before model work.
        prepared["item"]["current"]["prices"][0]["amount"] = "1"
        runner = Runner()
        with self.assertRaisesRegex(SafetyError, "MATERIAL_UPDATE_INPUT_INVALID"):
            run_material_updates(self.memory, [prepared], runner, as_of=NOW)
        self.assertEqual(runner.calls, 0)

    def test_newer_unpublished_reservation_does_not_erase_actual_baseline(self):
        before = latest_published_baseline(self.memory.snapshot(), "c000", "default", NOW)
        old = self.memory.snapshot()["issue_reservations"]["original"]
        newer = dict(deepcopy(old), revision=2, state="reserved", products=[])
        self.memory.put("issue_reservations", "original", newer, previous=old)
        after = latest_published_baseline(self.memory.snapshot(), "c000", "default", NOW)
        self.assertEqual(before, after)
        self.assertIsNone(after["reservation_hash"])

    def test_actual_changed_price_runs_one_saved_E_and_becomes_updated(self):
        reduce_price(self.memory)
        prepared = question(self.memory)
        runner = Runner()
        outcomes = run_material_updates(self.memory, [prepared], runner, as_of=NOW)
        self.assertEqual(outcomes[0]["decision"], "useful_update")
        self.assertEqual(runner.calls, 1)
        self.assertEqual(prompt_version("E", update_payload([prepared["item"]])), UPDATE_PROMPT_VERSION)
        row = build_report_pool(self.memory, as_of=NOW)["eligible"][0]
        self.assertEqual(row["status"], "Updated")
        self.assertEqual(row["material_update_review"]["rule"], REVIEW_KIND)
        self.assertTrue(update_baseline_current(row, self.memory.snapshot()["published_history"],
            self.memory.snapshot()["issue_reservations"], "default"))
        before = self.memory.snapshot()
        self.assertEqual(run_material_updates(self.memory, [prepared], runner, as_of=NOW)[0]["state"], "reused")
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(runner.calls, 1)

    def test_not_material_and_unclear_are_saved_without_invented_consequence(self):
        for decision in ("not_material", "unclear"):
            with self.subTest(decision=decision):
                memory = saved_fixture(self.root / decision)
                published(memory); reduce_price(memory)
                prepared = question(memory)
                runner = Runner(decision=decision)
                # Distinct runner identity is an explicit synthetic owner.
                runner.identity += decision
                run_material_updates(memory, [prepared], runner, as_of=NOW)
                pool = build_report_pool(memory, as_of=NOW)
                self.assertFalse(any(row["concept_id"] == "c000" for row in pool["eligible"]))
                self.assertFalse(any(row["binding"]["concept_id"] == "c000" for row in pool["needed_material_update_work"]))
                self.assertEqual(runner.calls, 1)

    def test_saved_verdict_cannot_replace_the_actual_accepted_original_E_answer(self):
        reduce_price(self.memory)
        prepared = question(self.memory)
        run_material_updates(self.memory, [prepared], Runner(decision="not_material"), as_of=NOW)
        state = self.memory.snapshot()
        rid, original = next((rid, row) for rid, row in state["source_checks"].items()
            if row.get("kind") == REVIEW_KIND and row["binding"] == prepared["binding"])
        forged = deepcopy(original)
        forged["result"].update(decision="useful_update", practical_consequence="Recheck this lower quoted cost.")
        forged["record_hash"] = digest({k: v for k, v in forged.items() if k != "record_hash"})
        state["source_checks"][rid] = forged
        with self.assertRaisesRegex(SafetyError, "SAVED_MATERIAL_UPDATE_INVALID"):
            current_material_update(state, prepared, as_of=NOW)

    def test_rehashing_receipt_capsule_and_verdict_cannot_keep_old_review_identity(self):
        reduce_price(self.memory); prepared = question(self.memory)
        run_material_updates(self.memory, [prepared], Runner(decision="not_material"), as_of=NOW)
        state = self.memory.snapshot()
        rid, original = next((rid, row) for rid, row in state["source_checks"].items() if row.get("kind") == REVIEW_KIND)
        forged = deepcopy(original)
        forged["result"].update(decision="useful_update", practical_consequence="Recheck this lower quoted cost.")
        proof = forged["job_proof"]
        proof["accepted_item"] = deepcopy(forged["result"])
        proof["accepted_item_hash"] = digest(proof["accepted_item"])
        proof["result_receipt"]["response"]["results"] = [deepcopy(forged["result"])]
        proof["result_hash"] = digest(proof["result_receipt"])
        forged["record_hash"] = digest({k: v for k, v in forged.items() if k != "record_hash"})
        state["source_checks"][rid] = forged
        with self.assertRaisesRegex(SafetyError, "SAVED_MATERIAL_UPDATE_INVALID"):
            current_material_update(state, prepared, as_of=NOW)

    def test_historical_acceptance_reads_without_live_result_file_or_model_repeat(self):
        from product_radar.research_ai import current_attempt
        reduce_price(self.memory); prepared = question(self.memory); runner = Runner()
        run_material_updates(self.memory, [prepared], runner, as_of=NOW)
        saved = current_material_update(self.memory.snapshot(), prepared, as_of=NOW)
        path = current_attempt(self.memory.root / "ai_jobs" / saved["job_id"]) / "result.json"
        path.unlink()  # Only this test's temporary synthetic file.
        self.assertEqual(current_material_update(self.memory.snapshot(), prepared, as_of=NOW), saved)
        self.assertEqual(run_material_updates(self.memory, [prepared], runner, as_of=NOW)[0]["state"], "reused")
        self.assertEqual(runner.calls, 1)

    def test_removed_proof_cannot_be_reported_as_accepted_without_repair(self):
        reduce_price(self.memory); prepared = question(self.memory); runner = Runner()
        run_material_updates(self.memory, [prepared], runner, as_of=NOW)
        rid, original = next((rid, row) for rid, row in self.memory.snapshot()["source_checks"].items()
            if row.get("kind") == REVIEW_KIND)
        broken = deepcopy(original); broken.pop("job_proof")
        broken["record_hash"] = digest({k: v for k, v in broken.items() if k != "record_hash"})
        self.memory.put("source_checks", rid, broken, previous=original)
        self.assertIsNone(current_material_update(self.memory.snapshot(), prepared, as_of=NOW))
        with self.assertRaisesRegex(SafetyError, "SAVED_MATERIAL_UPDATE_INVALID"):
            run_material_updates(self.memory, [prepared], runner, as_of=NOW)
        self.assertEqual(runner.calls, 1)

    def test_actual_original_result_change_during_commit_blocks_acceptance(self):
        from product_radar.storage import atomic_json, load_json
        reduce_price(self.memory); prepared = question(self.memory); runner = Runner()
        original = self.memory.commit
        def changed(event_id, changes, **kwargs):
            if event_id.startswith("apply-material-update-"):
                proof = changes[0]["value"]["job_proof"]
                path = self.memory.root / "ai_jobs" / proof["job_id"] / "result.json"
                receipt = load_json(path)
                receipt["usage"] = {"synthetic": "changed before local commit"}
                atomic_json(path, receipt)
            return original(event_id, changes, **kwargs)
        with patch.object(self.memory, "commit", side_effect=changed):
            result = run_material_updates(self.memory, [prepared], runner, as_of=NOW)
        self.assertEqual(result[0]["reason"], "MATERIAL_UPDATE_ORIGINAL_RESULT_CHANGED")
        self.assertIsNone(current_material_update(self.memory.snapshot(), prepared, as_of=NOW))
        self.assertEqual(runner.calls, 1)

    def test_changed_original_job_binding_during_commit_blocks_acceptance(self):
        reduce_price(self.memory); prepared = question(self.memory); runner = Runner()
        original = self.memory.commit
        def changed(event_id, changes, **kwargs):
            if event_id.startswith("apply-material-update-"):
                job_id = changes[0]["value"]["job_id"]
                old = self.memory.snapshot()["jobs"][job_id]
                forged = deepcopy(old); forged["binding_keys"][prepared["item"]["id"]] = "f" * 64
                self.memory.put("jobs", job_id, forged, previous=old)
            return original(event_id, changes, **kwargs)
        with patch.object(self.memory, "commit", side_effect=changed):
            result = run_material_updates(self.memory, [prepared], runner, as_of=NOW)
        self.assertEqual(result[0]["reason"], "MATERIAL_UPDATE_ORIGINAL_ACCEPTANCE_INVALID")
        self.assertIsNone(current_material_update(self.memory.snapshot(), prepared, as_of=NOW))
        self.assertEqual(runner.calls, 1)

    def legacy_application(self):
        from product_radar.research_memory import key
        reduce_price(self.memory); prepared = question(self.memory); runner = Runner()
        job_id, outcome = Jobs(self.memory, runner).run("E", update_payload([prepared["item"]]),
            declared_version=UPDATE_PROMPT_VERSION)
        legacy = {"kind": REVIEW_KIND, "binding": deepcopy(prepared["binding"]),
            "binding_hash": digest(prepared["binding"]), "input": deepcopy(prepared["item"]),
            "job_id": job_id, "result": outcome["accepted"][prepared["item"]["id"]], "reviewed_at": NOW}
        legacy["record_hash"] = digest(legacy)
        legacy_id = key("material-update-review", [prepared["binding"], job_id])
        self.memory.put("source_checks", legacy_id, legacy)
        return prepared, runner, legacy_id, legacy

    def test_old_application_is_hydrated_from_exact_saved_reply_without_overwrite(self):
        prepared, runner, legacy_id, legacy = self.legacy_application()
        self.assertIsNone(current_material_update(self.memory.snapshot(), prepared, as_of=NOW))
        result = run_material_updates(self.memory, [prepared], runner, as_of=NOW)
        self.assertEqual(result[0]["state"], "accepted")
        self.assertNotEqual(result[0]["review_id"], legacy_id)
        self.assertEqual(self.memory.snapshot()["source_checks"][legacy_id], legacy)
        self.assertIsNotNone(current_material_update(self.memory.snapshot(), prepared, as_of=NOW))
        self.assertEqual(runner.calls, 1)

    def test_missing_old_artifact_folder_does_not_launch_a_second_model_request(self):
        prepared, runner, legacy_id, legacy = self.legacy_application()
        folder = self.memory.root / "ai_jobs" / legacy["job_id"]
        for filename in ("result.json", "intent.json"):
            (folder / filename).unlink()  # Only this test's synthetic files.
        result = run_material_updates(self.memory, [prepared], runner, as_of=NOW)
        self.assertEqual(result[0]["state"], "waiting")
        self.assertEqual(result[0]["reason"], "MATERIAL_UPDATE_ORIGINAL_RESULT_UNAVAILABLE")
        self.assertEqual(runner.calls, 1)
        self.assertEqual(self.memory.snapshot()["source_checks"][legacy_id], legacy)
        self.assertIsNone(current_material_update(self.memory.snapshot(), prepared, as_of=NOW))

    def test_old_completed_reply_is_recovered_locally_without_execution_or_admission(self):
        from product_radar.storage import atomic_json, load_json
        prepared, runner, legacy_id, legacy = self.legacy_application()
        folder = self.memory.root / "ai_jobs" / legacy["job_id"]
        receipt = load_json(folder / "result.json")
        atomic_json(folder / "completion.json", {"response": receipt["response"], "usage": receipt["usage"], "exit_code": 0})
        (folder / "result.json").unlink()
        turn = open_work_turn(self.memory, "legacy-hydration", at=NOW,
            policy=dict(deepcopy(DEFAULT_POLICY), model_executions=1, report_slots=0))
        with bind_work_turn(self.memory, turn["id"]):
            result = run_material_updates(self.memory, [prepared], runner, as_of=NOW)
        self.assertEqual(result[0]["state"], "accepted")
        actual_turn = self.memory.snapshot()["batch_runs"][turn["id"]]
        self.assertEqual(actual_turn["counters"]["model_executions"], 0)
        self.assertEqual(runner.calls, 1)
        self.assertEqual(self.memory.snapshot()["source_checks"][legacy_id], legacy)
        self.assertIsNotNone(current_material_update(self.memory.snapshot(), prepared, as_of=NOW))

    def test_original_larger_batch_is_recovered_not_aggregate_or_new_execution(self):
        reduce_price(self.memory); reduce_price(self.memory, n=1)
        first, second = question(self.memory), question(self.memory, 1); runner = Runner()
        original_id, _ = Jobs(self.memory, runner).run("E", update_payload([first["item"], second["item"]]),
            declared_version=UPDATE_PROMPT_VERSION)
        result = run_material_updates(self.memory, [first], runner, as_of=NOW)
        self.assertEqual(result[0]["state"], "accepted")
        saved = current_material_update(self.memory.snapshot(), first, as_of=NOW)
        self.assertEqual(saved["job_id"], original_id)
        self.assertEqual(len(saved["job_proof"]["original_job"]["request"]["payload"]["items"]), 2)
        self.assertEqual(runner.calls, 1)

    def test_later_actual_model_completion_is_not_rejected_by_round_start_time(self):
        from datetime import timedelta
        from product_radar.models import timestamp
        reduce_price(self.memory); prepared = question(self.memory); runner = Runner()
        later = (timestamp(NOW) + timedelta(minutes=1)).isoformat()
        with patch("product_radar.research_ai.utc_now", return_value=later):
            result = run_material_updates(self.memory, [prepared], runner, as_of=NOW)
        self.assertEqual(result[0]["state"], "accepted")
        saved = current_material_update(self.memory.snapshot(), prepared, as_of=NOW)
        self.assertEqual(saved["job_proof"]["result_receipt"]["completed_at"], later)
        self.assertEqual(runner.calls, 1)

    def test_changed_current_attempt_during_application_cannot_adopt_old_reply(self):
        from product_radar.storage import atomic_json
        reduce_price(self.memory); prepared = question(self.memory); runner = Runner()
        original = self.memory.commit
        def changed(event_id, changes, **kwargs):
            if event_id.startswith("apply-material-update-"):
                job_id = changes[0]["value"]["job_id"]
                atomic_json(self.memory.root / "ai_jobs" / job_id / "retries.json", [{"reason": "Synthetic pointer race"}])
            return original(event_id, changes, **kwargs)
        with patch.object(self.memory, "commit", side_effect=changed):
            result = run_material_updates(self.memory, [prepared], runner, as_of=NOW)
        self.assertEqual(result[0]["reason"], "MATERIAL_UPDATE_ORIGINAL_RESULT_CHANGED")
        self.assertIsNone(current_material_update(self.memory.snapshot(), prepared, as_of=NOW))
        self.assertEqual(runner.calls, 1)

    def test_wrong_reference_is_refused_and_original_reply_is_not_repeated(self):
        reduce_price(self.memory)
        prepared = question(self.memory); runner = Runner(bad=True)
        result = run_material_updates(self.memory, [prepared], runner, as_of=NOW)
        self.assertEqual(result[0]["state"], "waiting")
        self.assertIsNone(current_material_update(self.memory.snapshot(), prepared, as_of=NOW))
        run_material_updates(self.memory, [prepared], runner, as_of=NOW)
        self.assertEqual(runner.calls, 1)

    def test_uncertain_original_is_not_relaunched(self):
        reduce_price(self.memory)
        prepared = question(self.memory); runner = Runner(uncertain=True)
        run_material_updates(self.memory, [prepared], runner, as_of=NOW)
        run_material_updates(self.memory, [prepared], runner, as_of=NOW)
        self.assertEqual(runner.calls, 1)

    def test_lost_application_commit_recovers_original_without_model_repeat(self):
        reduce_price(self.memory)
        prepared = question(self.memory); runner = Runner()
        original = self.memory.commit
        def interrupted(event_id, changes, **kwargs):
            if event_id.startswith("apply-material-update-"):
                raise SafetyError("SAVED_FILE_UNAVAILABLE")
            return original(event_id, changes, **kwargs)
        with patch.object(self.memory, "commit", side_effect=interrupted):
            result = run_material_updates(self.memory, [prepared], runner, as_of=NOW)
        self.assertEqual(result[0]["state"], "waiting")
        self.assertEqual(run_material_updates(self.memory, [prepared], runner, as_of=NOW)[0]["state"], "accepted")
        self.assertEqual(runner.calls, 1)

    def test_changed_original_during_application_commit_blocks_acceptance(self):
        reduce_price(self.memory)
        prepared = question(self.memory); runner = Runner()
        original = self.memory.commit
        def changed(event_id, changes, **kwargs):
            if event_id.startswith("apply-material-update-"):
                reduce_price(self.memory, amount=90)
            return original(event_id, changes, **kwargs)
        with patch.object(self.memory, "commit", side_effect=changed):
            result = run_material_updates(self.memory, [prepared], runner, as_of=NOW)
        self.assertEqual(result[0]["reason"], "MATERIAL_UPDATE_DEPENDENCY_CHANGED")
        self.assertIsNone(current_material_update(self.memory.snapshot(), prepared, as_of=NOW))

    def test_new_capture_id_seller_colour_date_and_order_do_not_create_work(self):
        pool = build_report_pool(self.memory, as_of=NOW)
        self.assertEqual(pool["needed_material_update_work"], [])
        snapshot = self.issue["products"][0]["material_facts"]
        fact = deepcopy(pool["facts_by_concept"]["c000"])
        fact.update(title="Blue version", source_refs=[{"id": "a-new-capture"}], image_path="new.png")
        fact["suppliers"][0].update(name="Other seller", source_capture_at=NOW, reviewed_at=NOW)
        market = {"label": snapshot["market"]["label"], "families": snapshot["market"]["families"]}
        self.assertEqual(material_snapshot(fact, market), snapshot)

    def test_latest_new_real_publication_invalidates_the_saved_verdict(self):
        reduce_price(self.memory)
        prepared = question(self.memory); run_material_updates(self.memory, [prepared], Runner(), as_of=NOW)
        state = self.memory.snapshot(); rid, old = next((rid, row) for rid, row in state["published_history"].items() if row["concept_id"] == "c000")
        self.memory.put("published_history", "newer-exposure", dict(deepcopy(old), available_at=NOW))
        self.assertIsNone(current_material_update(self.memory.snapshot(), prepared, as_of=NOW))

    def test_shared_turn_model_limit_and_exact_result_reuse(self):
        reduce_price(self.memory); reduce_price(self.memory, n=1)
        first, second = question(self.memory), question(self.memory, 1)
        runner = Runner()
        turn = open_work_turn(self.memory, "material-test", at=NOW,
            policy=dict(deepcopy(DEFAULT_POLICY), model_executions=1, report_slots=0))
        with bind_work_turn(self.memory, turn["id"]):
            rows = run_material_updates(self.memory, [first, second], runner, as_of=NOW)
        self.assertEqual(rows[0]["state"], "accepted")
        self.assertEqual(rows[1]["reason"], "WORK_TURN_MODEL_LIMIT")
        self.assertEqual(runner.calls, 1)
        close_work_turn(self.memory, turn["id"], at=NOW, reason="finite test complete")

    def test_changed_semantic_head_reuses_same_E_after_presentation_only_refresh(self):
        reduce_price(self.memory)
        first = question(self.memory); runner = Runner()
        run_material_updates(self.memory, [first], runner, as_of=NOW)
        # The exact price/facts remain unchanged, but a new original exists.
        reduce_price(self.memory)
        second = question(self.memory)
        self.assertNotEqual(first["binding"], second["binding"])
        self.assertEqual(first["item"], second["item"])
        self.assertEqual(run_material_updates(self.memory, [second], runner, as_of=NOW)[0]["state"], "accepted")
        self.assertEqual(runner.calls, 1)

    def test_actual_E_update_allocation_keeps_eight_new_and_two_updated(self):
        memory = saved_fixture(self.root / "cap", count=18)
        published(memory, concept_ids={f"c{n:03d}" for n in range(10)})
        # Published history must survive release hold cleanup; an active hold
        # would intentionally prevent the same exact products being selected.
        old = memory.snapshot()["issue_reservations"]["original"]
        memory.put("issue_reservations", "original", dict(old, state="cancelled"), previous=old)
        reduce_price(memory); reduce_price(memory, n=1)
        runner = Runner()
        run_material_updates(memory, [question(memory), question(memory, 1)], runner, as_of=NOW)
        pool = build_report_pool(memory, as_of=NOW)
        state = memory.snapshot()
        issue = allocate_issues(pool["eligible"], ["2026-10-06"], state["published_history"], state["issue_reservations"])["issues"][0]
        self.assertEqual(issue["counts"], {"products": 10, "new": 8, "updates": 2})
        self.assertTrue(issue["ready_for_review"])
        self.assertEqual(runner.calls, 2)

    def test_real_legacy_alias_cannot_be_false_new_and_compares_original_publication(self):
        memory = saved_fixture(self.root / "alias", count=11)
        published(memory, concept_ids={f"c{n:03d}" for n in range(10)})
        state = memory.snapshot(); old = state["concepts"]["c000"]
        memory.put("concepts", "c000", dict(old, status="merged", merged_into="c010"), previous=old)
        baseline = latest_published_baseline_for_members(memory.snapshot(), "c010", "default", NOW,
            member_ids=["c000", "c010"])
        self.assertEqual(baseline["snapshot"]["concept_id"], "c000")
        self.assertFalse(any(r["concept_id"] == "c010" for r in build_report_pool(memory, as_of=NOW)["eligible"]))
        reduce_price(memory, n=10)
        pool = build_report_pool(memory, as_of=NOW)
        prepared = next(r for r in pool["needed_material_update_work"] if r["binding"]["concept_id"] == "c010")
        self.assertEqual(prepared["item"]["comparison_subjects"], ["c000", "c010"])
        run_material_updates(memory, [prepared], Runner(), as_of=NOW)
        row = next(r for r in build_report_pool(memory, as_of=NOW)["eligible"] if r["concept_id"] == "c010")
        self.assertEqual(row["status"], "Updated")
        state = memory.snapshot()
        self.assertTrue(update_baseline_current(row, state["published_history"], state["issue_reservations"], "default",
            member_ids=["c000", "c010"]))
        self.assertFalse(update_baseline_current(row, state["published_history"], state["issue_reservations"], "default"))

    def test_existing_programme_fit_lane_applies_bounded_E_update_under_shared_turn(self):
        from product_radar.programme import _review_material_updates
        reduce_price(self.memory)
        record = {"concept_ids": ["c000"], "client_id": "default", "drafts": [], "source_run_id": "unused",
            "follow_on_source_run_ids": [], "discovery_cycle_ids": [], "deferrals": {}}
        runner = Runner()
        context = {"at": NOW, "runner": runner, "fit_limit": 10}
        turn = open_work_turn(self.memory, "programme-material-test", at=NOW)
        with bind_work_turn(self.memory, turn["id"]):
            result = _review_material_updates(self.memory, record, context)
        self.assertEqual(result["selected"], 1)
        self.assertEqual(result["outcomes"][0]["state"], "accepted")
        self.assertEqual(runner.calls, 1)
        close_work_turn(self.memory, turn["id"], at=NOW, reason="finite synthetic programme test")

    def test_older_published_type_enters_later_intake_and_resumes_after_progress_commit_loss(self):
        from product_radar.inventory_intake import refresh_inventory_intake, reporting_intake_concept_ids
        from product_radar.programme import prepare_programme, _run_inventory_stage, _programme_report_pool
        from product_radar.fit_review import prepare_practical_fit_reviews, run_fit_reviews
        from product_radar.fit_description import DESCRIPTION_CONTRACT
        from test_fit_description import DescriptionRunner
        bases = prepare_practical_fit_reviews(self.memory, concept_ids=["c000"], as_of=NOW,
            contract_version=DESCRIPTION_CONTRACT)
        result = run_fit_reviews(self.memory, DescriptionRunner(), [row["review_id"] for row in bases])
        self.assertTrue(all(row["status"] == "accepted" for row in result))
        reduce_price(self.memory)
        later = prepare_programme(self.memory, "later-material-intake", concept_ids=[], as_of=NOW)
        prepared = refresh_inventory_intake(self.memory, later, as_of=NOW,
            pool=_programme_report_pool(self.memory, later, as_of=NOW))
        later.update({field: prepared[field] for field in ("inventory_work_ids", "reporting_intake_ids")})
        rows = [self.memory.snapshot()["batch_runs"][ref] for ref in later["inventory_work_ids"]]
        work = next(row for row in rows if row["binding"]["purpose"] == "material_update")
        later["inventory_work_ids"] = [work["id"]]
        self.assertEqual(work["binding"]["subject"]["concept_id"], "c000")
        self.assertEqual(later["concept_ids"], [])
        original_scope = deepcopy(later["concept_ids"])
        runner = Runner(); original = self.memory.commit
        context = {"at": NOW, "runner": runner, "fit_limit": 10, "retry_seconds": 300}
        turn = open_work_turn(self.memory, later["programme_id"], at=NOW)
        def lost_progress(event_id, changes, **kwargs):
            if event_id.startswith("inventory-work-progress-"):
                raise SafetyError("SAVED_FILE_UNAVAILABLE")
            return original(event_id, changes, **kwargs)
        with bind_work_turn(self.memory, turn["id"]):
            with patch.object(self.memory, "commit", side_effect=lost_progress):
                with self.assertRaisesRegex(SafetyError, "SAVED_FILE_UNAVAILABLE"):
                    _run_inventory_stage(self.memory, later, context, "fit")
            self.assertEqual(runner.calls, 1)
            _run_inventory_stage(self.memory, later, context, "fit")
        self.assertEqual(runner.calls, 1)
        self.assertEqual(self.memory.snapshot()["batch_runs"][work["id"]]["progress"]["state"], "complete")
        refreshed = refresh_inventory_intake(self.memory, later, as_of=NOW,
            pool=_programme_report_pool(self.memory, later, as_of=NOW))
        later.update({field: refreshed[field] for field in ("inventory_work_ids", "reporting_intake_ids")})
        self.assertIn("c000", reporting_intake_concept_ids(self.memory.snapshot(), later))
        self.assertEqual(later["concept_ids"], original_scope)
        close_work_turn(self.memory, turn["id"], at=NOW, reason="finite synthetic rolling update")

    def test_old_update_obligation_cannot_review_different_new_question(self):
        from product_radar.inventory_intake import refresh_inventory_intake
        from product_radar.programme import prepare_programme, _run_inventory_stage, _programme_report_pool
        reduce_price(self.memory)
        later = prepare_programme(self.memory, "changed-material-intake", concept_ids=[], as_of=NOW)
        prepared = refresh_inventory_intake(self.memory, later, as_of=NOW,
            pool=_programme_report_pool(self.memory, later, as_of=NOW))
        later.update({field: prepared[field] for field in ("inventory_work_ids", "reporting_intake_ids")})
        work_id = next(ref for ref in later["inventory_work_ids"]
            if self.memory.snapshot()["batch_runs"][ref]["binding"]["purpose"] == "material_update")
        later["inventory_work_ids"] = [work_id]
        reduce_price(self.memory, amount=90)
        runner = Runner(); turn = open_work_turn(self.memory, later["programme_id"], at=NOW)
        with bind_work_turn(self.memory, turn["id"]):
            _run_inventory_stage(self.memory, later, {"at": NOW, "runner": runner, "fit_limit": 10}, "fit")
        self.assertEqual(runner.calls, 0)
        self.assertEqual(self.memory.snapshot()["batch_runs"][work_id]["progress"]["state"], "superseded")
        close_work_turn(self.memory, turn["id"], at=NOW, reason="finite changed obligation")

    def test_useful_update_requires_both_old_and_new_changed_fact_references(self):
        reduce_price(self.memory); prepared = question(self.memory)
        item = prepared["item"]
        value = {"id": item["id"], "decision": "useful_update", "reason": "Meaningful change.",
            "practical_consequence": "Recheck costs.", "changed_fields": ["prices"], "references": ["new:prices"]}
        with self.assertRaisesRegex(SafetyError, "MATERIAL_UPDATE_RESULT_INVALID"):
            validate_update_result(item, value)


@older_format_scores
class ScopedMaterialSnapshotTests(unittest.TestCase):
    def fact(self):
        return {"concept_id": "actual-type", "recommendation_contract": "functional-type-examples-v1",
            "functional_definition": {"meaning_hash": "a" * 64}, "criteria": [], "prices": [], "source_route_checks": [],
            "functional_market_proof": {}, "supplier_scoped_terms": {"all": []}}

    def test_all_seven_sources_are_preserved_without_three_family_collapse(self):
        fact = self.fact()
        market = {"sources": {source: {"available": True, "positive": source == "zepto", "used_ids": ["irrelevant-proof-id"]}
            for source in ("meta", "amazon", "flipkart", "google-trends", "blinkit", "zepto", "instamart")}}
        snapshot = material_snapshot(fact, market)
        self.assertEqual(snapshot["version"], SCOPED_VERSION)
        self.assertEqual(len(snapshot["market"]["sources"]), 7)
        self.assertTrue(snapshot["market"]["sources"]["zepto"]["signal"]["positive"])
        self.assertNotIn("irrelevant-proof-id", str(snapshot))

    def test_actual_functional_capture_and_scope_producers_create_v2_snapshot(self):
        from test_functional_source_market import functional_pool_fixture, NOW as FUNCTIONAL_NOW
        from product_radar.research_flow import candidate_summary
        from product_radar.report_examples import RECOMMENDATION_CONTRACT
        with tempfile.TemporaryDirectory() as folder:
            memory, (cid,) = functional_pool_fixture(Path(folder))
            pool = build_report_pool(memory, as_of=FUNCTIONAL_NOW, recommendation_contract=RECOMMENDATION_CONTRACT)
            fact = pool["facts_by_concept"][cid]
            product = next(row for row in candidate_summary(memory, as_of=FUNCTIONAL_NOW)["products"] if row["id"] == cid)
            snapshot = material_snapshot(fact, product["market"])
            self.assertEqual(snapshot["version"], SCOPED_VERSION)
            self.assertEqual(snapshot, pool["eligible"][0]["material_facts"])
            self.assertEqual(snapshot["meaning_hash"], fact["functional_definition"]["meaning_hash"])
            self.assertEqual(set(snapshot["market"]["sources"]),
                {"meta", "amazon", "flipkart", "google-trends", "blinkit", "zepto", "instamart"})
            self.assertTrue(snapshot["market"]["sources"]["amazon"]["scopes"])

    def test_changed_number_without_same_query_location_scope_is_not_comparable(self):
        old = material_snapshot(self.fact(), {"sources": {"zepto": {"available": True, "positive": False}}})
        new = deepcopy(old); new["market"]["sources"]["zepto"]["signal"]["positive"] = True
        changes = find_material_changes(old, new)
        self.assertFalse(comparable_changes(old, new, changes)["comparable"])
        for row in (old, new):
            row["market"]["sources"]["zepto"]["scopes"] = [{"route": "zepto", "query": "sink caddy", "location": {"name": "Delhi"}}]
        self.assertTrue(comparable_changes(old, new, find_material_changes(old, new))["comparable"])
        new["market"]["sources"]["zepto"]["scopes"][0]["location"]["name"] = "Mumbai"
        self.assertFalse(comparable_changes(old, new, find_material_changes(old, new))["comparable"])

    def test_different_pack_and_scope_versions_cannot_be_same_price_comparison(self):
        old = material_snapshot(self.fact(), {"sources": {}})
        old["prices"] = [{"amount": "100", "currency": "INR", "unit": "pack of 3", "moq": 100}]
        new = deepcopy(old); new["prices"] = [{"amount": "90", "currency": "INR", "unit": "piece", "moq": 1}]
        self.assertFalse(comparable_changes(old, new, find_material_changes(old, new))["comparable"])
        new["version"] = "material-facts-v1"
        self.assertFalse(comparable_changes(old, new, find_material_changes(old, new))["comparable"])

    def test_only_checklist_judgment_changed_is_not_material_work(self):
        old = material_snapshot(self.fact(), {"sources": {}})
        new = deepcopy(old); new["criteria"] = [{"id": 1, "state": "Meets", "basis": "inferred"}]
        self.assertFalse(comparable_changes(old, new, find_material_changes(old, new))["comparable"])

    def test_trends_context_preserves_actual_window_but_not_new_capture_date(self):
        from product_radar.frozen_records import freeze_original_json
        fact = self.fact()
        row = {"source": "trends", "scope": "narrow_type", "admissibility": "accepted", "route": "google-trends",
            "source_contexts": [{"request": {"term": "sink caddy", "geo": "IN", "time_range": "today 12-m",
                "property": "", "category": 0, "collected_at": NOW, "source_reference": "old-file"}}]}
        fact["functional_market_proof"] = {"scoped_records": {"old-scope": freeze_original_json(row)}}
        market = {"sources": {"google-trends": {"available": True, "positive": True, "ratio": 1.2}}}
        old = material_snapshot(fact, market)
        row["source_contexts"][0]["request"].update(collected_at="2026-10-07T10:00:00Z", source_reference="new-file")
        fact["functional_market_proof"]["scoped_records"] = {"new-scope": freeze_original_json(row)}
        self.assertEqual(material_snapshot(fact, market), old)
        market["sources"]["google-trends"]["ratio"] = 1.8
        current = material_snapshot(fact, market)
        self.assertTrue(comparable_changes(old, current, find_material_changes(old, current))["comparable"])
        self.assertEqual(old["market"]["sources"]["google-trends"]["scopes"][0]["request"]["geo"], "IN")

    def test_reference_choices_exclude_other_noncomparable_site_or_price_conditions(self):
        old = material_snapshot(self.fact(), {"sources": {}})
        for source in ("amazon", "zepto"):
            old["market"]["sources"][source] = {"signal": {"available": True, "positive": True, "ratio": 1},
                "scopes": [{"query": "sink caddy", "location": "Delhi"}], "measurements": []}
        old["prices"] = [{"amount": "100", "currency": "INR", "unit": "piece"}]
        new = deepcopy(old)
        new["market"]["sources"]["amazon"]["signal"]["ratio"] = 1.4
        new["market"]["sources"]["zepto"]["scopes"][0]["location"] = "Mumbai"
        new["prices"] = [{"amount": "90", "currency": "INR", "unit": "piece"},
            {"amount": "20", "currency": "INR", "unit": "pack of 2"}]
        item = update_item({"baseline": {"snapshot": old}, "current": new, "changes": find_material_changes(old, new)})
        refs = {r["id"]: r["facts"] for r in item["reference_choices"]}
        self.assertEqual(set(refs["new:market"]["sources"]), {"amazon"})
        self.assertEqual(refs["new:prices"], [{"amount": "90", "currency": "INR", "unit": "piece"}])

    def test_route_name_alone_is_not_a_market_comparison_scope(self):
        old = material_snapshot(self.fact(), {"sources": {"amazon": {"available": True, "positive": False}}})
        old["market"]["sources"]["amazon"]["scopes"] = [{"route": "amazon-bestsellers"}]
        new = deepcopy(old); new["market"]["sources"]["amazon"]["signal"]["positive"] = True
        self.assertFalse(comparable_changes(old, new, find_material_changes(old, new))["comparable"])

    def test_supplier_clock_expiry_does_not_change_literal_scoped_terms(self):
        fact = self.fact()
        term = {"field": "single_piece_purchase", "value": "Yes", "basis": "observed", "applicability": "applies",
            "qualified": True, "current": True, "observed_at": NOW, "exceptions": []}
        fact["supplier_scoped_terms"]["all"] = [{"scope_kind": "product_offer", "purchase_unit": "piece",
            "relevance": {"relationship": "exact_product"}, "terms": [deepcopy(term)], "effective_terms": [deepcopy(term)],
            "prices": [], "fees": [], "min_order": 1, "min_order_unit": "piece", "supply_arrangement": "Per-order"}]
        old = material_snapshot(fact, {"sources": {}})
        row = fact["supplier_scoped_terms"]["all"][0]
        row["terms"][0].update(current=False, qualified=False)
        row.update(effective_terms=[], min_order=None, min_order_unit=None, supply_arrangement="Unknown")
        self.assertEqual(material_snapshot(fact, {"sources": {}}), old)


@older_format_scores
class MaterialIdentityWakeTests(unittest.TestCase):
    def test_only_a_relevant_actual_reviewed_alias_wakes_cooperative_work(self):
        from test_functional_aliases import FunctionalAliasTests
        from product_radar.programme import prepare_programme, _cooperative_signature
        fixture = FunctionalAliasTests(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        memory = fixture.memory
        record = prepare_programme(memory, "alias-material-wake", concept_ids=["bear"], as_of=NOW)
        before = _cooperative_signature(memory.snapshot(), record)
        fixture.accept(fixture.prepared(("bird", "cat")))
        self.assertEqual(_cooperative_signature(memory.snapshot(), record), before)
        fixture.accept(fixture.prepared(("bear", "bird")))
        self.assertNotEqual(_cooperative_signature(memory.snapshot(), record), before)


if __name__ == "__main__":
    unittest.main()
