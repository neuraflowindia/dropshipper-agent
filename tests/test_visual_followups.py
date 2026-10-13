"""Fixture-only saved-photo dispatch/application tests; no live model or fetch."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.discovery import find_possible_matches
from product_radar.models import SafetyError, digest
from product_radar.research_ai import Jobs, prepare_retry
from product_radar.research_memory import ResearchMemory, change, key
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.visual_followups import resolve_saved_visual_followups
from test_report_selection import NOW, fixture


class SavedVisualFollowupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.photos = self.root / "photos"
        self.photos.mkdir()
        patcher = patch("product_radar.research_ai.PUBLIC_PRODUCT_IMAGES", self.photos)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        state = fixture().state
        state["observations"]["meta000"]["semantic"]["original_image_url"] = "https://images.example.org/candidate.png"
        self.memory.commit("fixture", [change(table, i, row) for table, rows in state.items() for i, row in rows.items()])
        self.calls = []
        self.decision = "same"
        self.child = self.add_target("target")
        self.images = [self.photo(self.child, "obs-target", "target"), self.photo("c000", "meta000", "candidate")]

    def add_target(self, suffix, *, b_decision="unclear", mixed=False, product=None, candidates=None):
        state = self.memory.snapshot()
        if product is None:
            product = deepcopy(state["concepts"]["c000"]["description"])
            product["label"] += " variant " + suffix
        else:
            product = deepcopy(product)
        semantic_id, observation_id = "semantic-" + suffix, "obs-" + suffix
        answer = {"id": semantic_id, "kind": "mixed" if mixed else "product",
                  "products": [product, deepcopy(product)] if mixed else [product], "reason": "Fixture purchase unit."}
        child = key("child", [semantic_id, 0, digest(answer)])
        source = deepcopy(state["observations"]["meta000"])
        source.update(source_item="ad-" + suffix, semantic_id=semantic_id, observed_at=NOW,
                      semantic={"title": product["label"], "original_image_url": "https://images.example.org/" + suffix + ".png"})
        self.memory.commit("target-" + suffix, [
            change("observations", observation_id, source),
            change("descriptions", semantic_id, {"result": answer, "job_id": "fixture-a-" + suffix})])
        payload = {"items": [{"id": child, "description": product,
                              "candidates": find_possible_matches(product, state["concepts"])["candidates"]
                              if candidates is None else deepcopy(candidates)}]}
        def b_runner(role, data, folder):
            return {"results": [{"id": child, "decision": b_decision,
                                "match_id": "c000" if b_decision == "same" else None,
                                "reason": "Need visible structure."}]}, {"input_tokens": 1, "output_tokens": 1}
        job_id, _ = Jobs(self.memory, b_runner).run("B", payload)
        self.memory.put("associations", child, {"semantic_id": semantic_id, "concept_id": None,
            "status": "deferred", "reason": "Need visible structure.", "job_id": job_id})
        return child

    def photo(self, owner, observation_id, name):
        path = self.photos / (name + ".png")
        Image.new("RGB", (12, 8), "red" if name.startswith("target") else "blue").save(path)
        observation = self.memory.snapshot()["observations"][observation_id]
        return {"id": "photo-" + name, "owner_id": owner, "path": path.name,
                "sha256": sha256(path.read_bytes()).hexdigest(), "source_kind": "public_product_image",
                "source_ref": observation_id, "source_url": observation["semantic"]["original_image_url"],
                "observed_at": observation["observed_at"]}

    def runner(self, role, payload, folder):
        self.calls.append((role, deepcopy(payload)))
        item = payload["items"][0]
        target = item["image_ids"][0]
        pairs = [{"image_ids": [target, c["image_ids"][0]], "feature": "Fixture comparison of visible shape."}
                 for c in item["candidates"] if c["image_ids"]]
        answer = {"id": item["id"], "decision": self.decision,
                  "match_id": item["candidates"][0]["id"] if self.decision == "same" else None,
                  "reason": "Fixture-only judgment; not a real visual assessment.",
                  "matching_features": pairs[:1] if self.decision == "same" else [],
                  "differing_features": pairs if self.decision == "different" else []}
        return {"results": [answer]}, {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}

    def execute(self, *, images=None, child=None, runner=None):
        return resolve_saved_visual_followups(self.memory,
            [{"child_id": child or self.child, "images": self.images if images is None else images}],
            self.runner if runner is None else runner, as_of=NOW)

    def first(self, **kwargs):
        return self.execute(**kwargs)["results"][0]

    def add_candidates(self, count):
        extra = fixture(count + 1).state
        state = self.memory.snapshot()
        changes = []
        for table, rows in extra.items():
            for record_id, row in rows.items():
                if record_id not in state[table]:
                    if table == "observations" and record_id.startswith("meta"):
                        row["semantic"]["original_image_url"] = "https://images.example.org/" + record_id + ".png"
                    changes.append(change(table, record_id, row))
        self.memory.commit("extra-candidates-" + str(count), changes)

    def profile_question(self, *, count=1):
        from test_discovery_retrieval import profile
        if count > 1:
            self.add_candidates(count - 1)
        state = self.memory.snapshot()
        # Explicitly emulate an original B shortlist before weak-token repair.
        original = [{"id": "c" + str(n).zfill(3),
                     "description": state["concepts"]["c" + str(n).zfill(3)]["description"]}
                    for n in range(count)]
        child = self.add_target("profile", product=profile(), candidates=original)
        images = [self.photo(child, "obs-profile", "profile"), self.images[1]]
        return child, images, original

    def test_original_b_photograph_fallback_survives_no_strong_text_match(self):
        child, images, original = self.profile_question()
        before = self.memory.snapshot()
        description = before["descriptions"]["semantic-profile"]["result"]["products"][0]
        self.assertEqual(find_possible_matches(description, before["concepts"])["candidates"], [])
        b_id = before["associations"][child]["job_id"]
        b_bytes = (self.memory.root / "ai_jobs" / b_id / "result.json").read_bytes()
        self.assertEqual(self.first(child=child, images=images)["status"], "applied")
        after = self.memory.snapshot()
        self.assertEqual(after["associations"][child]["concept_id"], "c000")
        self.assertEqual(after["jobs"][b_id], before["jobs"][b_id])
        self.assertEqual((self.memory.root / "ai_jobs" / b_id / "result.json").read_bytes(), b_bytes)
        self.assertEqual([row["id"] for row in self.calls[-1][1]["items"][0]["candidates"]], ["c000"])
        self.assertEqual(original[0]["description"], after["concepts"]["c000"]["description"])
        self.assertEqual(self.first(child=child, images=images)["status"], "already_applied")
        self.assertEqual(len(self.calls), 1)

    def test_original_b_unphotographed_scope_prevents_false_complete_different(self):
        child, images, _ = self.profile_question(count=3)
        self.decision = "different"
        self.assertEqual(self.first(child=child, images=images)["status"], "incomplete_comparison")
        state = self.memory.snapshot()
        progress = state["associations"][child]["visual_followup"]
        self.assertEqual(progress["inputs"]["omitted_candidates"], 2)
        self.assertEqual(progress["inputs"]["comparison_scope"], ["c000", "c001", "c002"])
        self.assertNotIn(key("concept", child), state["concepts"])
        self.assertEqual(self.first(child=child, images=images)["status"], "incomplete_comparison")
        self.assertEqual(len(self.calls), 1)

    def test_changed_unphotographed_original_candidate_stays_in_incomplete_scope(self):
        child, images, _ = self.profile_question(count=2)
        old = self.memory.snapshot()["concepts"]["c001"]
        self.memory.put("concepts", "c001", dict(old, description=dict(old["description"], label="Changed purchase unit")), previous=old)
        self.decision = "different"
        self.assertEqual(self.first(child=child, images=images)["status"], "incomplete_comparison")
        self.assertEqual(self.memory.snapshot()["associations"][child]["visual_followup"]["inputs"]["omitted_candidates"], 1)

    def test_retired_original_photo_candidate_does_not_revive(self):
        child, images, _ = self.profile_question()
        old = self.memory.snapshot()["concepts"]["c000"]
        self.memory.put("concepts", "c000", dict(old, status="retired"), previous=old)
        self.assertEqual(self.first(child=child, images=images)["reason"], "VISUAL_PHOTO_OWNER_INVALID")
        self.assertEqual(self.calls, [])

    def test_changed_original_photo_description_cannot_use_old_b_question(self):
        child, images, _ = self.profile_question()
        old = self.memory.snapshot()["concepts"]["c000"]
        self.memory.put("concepts", "c000", dict(old, description=dict(old["description"], function="Changed purpose")), previous=old)
        self.assertEqual(self.first(child=child, images=images)["reason"], "VISUAL_ORIGINAL_CANDIDATE_CHANGED")
        self.assertEqual(self.calls, [])

    def test_fallback_does_not_admit_arbitrary_photographed_concept(self):
        child, images, _ = self.profile_question()
        self.add_candidates(1)
        images[1] = self.photo("c001", "meta001", "other-candidate")
        self.assertEqual(self.first(child=child, images=images)["reason"], "VISUAL_PHOTO_OWNER_INVALID")
        self.assertEqual(self.calls, [])

    def test_fallback_candidate_photo_replacement_still_rejected(self):
        child, images, _ = self.profile_question()
        original = self.memory.snapshot()["observations"]["meta000"]
        self.memory.put("observations", "replacement-candidate", dict(original, semantic_id="replacement",
            semantic={"title": "Replaced listing", "original_image_url": "https://images.example.org/replaced.png"}))
        self.assertEqual(self.first(child=child, images=images)["reason"], "VISUAL_PHOTO_SOURCE_REPLACED")
        self.assertEqual(self.calls, [])

    def test_fallback_current_source_association_remains_bound_before_and_after_d(self):
        child, images, _ = self.profile_question()
        def changed(role, payload, folder):
            response = self.runner(role, payload, folder)
            state = self.memory.snapshot()
            source_child, old = next((i, a) for i, a in state["associations"].items() if a.get("concept_id") == "c000")
            self.memory.put("associations", source_child, dict(old, status="deferred", concept_id=None), previous=old)
            return response
        self.assertEqual(self.first(child=child, images=images, runner=changed)["status"], "inputs_changed")
        self.assertEqual(self.memory.snapshot()["associations"][child]["status"], "deferred")

    def test_fallback_three_photographed_candidates_keep_four_image_bound(self):
        child, images, _ = self.profile_question(count=4)
        images.extend([self.photo("c001", "meta001", "extra1"), self.photo("c002", "meta002", "extra2")])
        self.assertEqual(self.first(child=child, images=images)["status"], "applied")
        item = self.calls[-1][1]["items"][0]
        self.assertEqual([row["id"] for row in item["candidates"]], ["c000", "c001", "c002"])
        self.assertEqual(len(self.calls[-1][1]["images"]), 4)
        self.assertEqual(self.memory.snapshot()["associations"][child]["visual_followup"]["inputs"]["omitted_candidates"], 1)

    def test_fallback_union_scope_deduplicates_original_and_current_candidates(self):
        self.add_candidates(3)
        state = self.memory.snapshot()
        original = [{"id": "c" + str(n).zfill(3),
                     "description": state["concepts"]["c" + str(n).zfill(3)]["description"]}
                    for n in range(3)]
        child = self.add_target("union", candidates=original)
        images = [self.photo(child, "obs-union", "union"), self.images[1]]
        self.assertEqual(self.first(child=child, images=images)["status"], "applied")
        inputs = self.memory.snapshot()["associations"][child]["visual_followup"]["inputs"]
        self.assertEqual(inputs["comparison_scope"], ["c000", "c001", "c002", "c003"])
        self.assertEqual(inputs["omitted_candidates"], 1)

    def test_fallback_unrelated_question_keeps_prior_d_failure_and_b_receipts(self):
        def failed(role, payload, folder):
            self.calls.append((role, deepcopy(payload)))
            raise SafetyError("AI_TEMPORARY_EXECUTION_FAILURE")
        old = self.first(runner=failed)
        self.assertEqual(old["status"], "failed")
        state = self.memory.snapshot()
        job_id = old["job_id"]
        before_job = deepcopy(state["jobs"][job_id])
        failure = self.memory.root / "ai_jobs" / job_id / "failure.json"
        before_failure = failure.read_bytes()
        before_progress = deepcopy(state["associations"][self.child]["visual_followup"])
        child, images, _ = self.profile_question()
        self.assertEqual(self.first(child=child, images=images)["status"], "applied")
        after = self.memory.snapshot()
        self.assertEqual(after["jobs"][job_id], before_job)
        self.assertEqual(failure.read_bytes(), before_failure)
        self.assertEqual(after["associations"][self.child]["visual_followup"], before_progress)

    def test_same_attaches_preserving_b_provenance_without_other_fact_changes(self):
        before = self.memory.snapshot()
        result = self.first()
        self.assertEqual(result["status"], "applied")
        after = self.memory.snapshot()
        association = after["associations"][self.child]
        self.assertEqual(association["concept_id"], "c000")
        self.assertEqual(association["job_id"], before["associations"][self.child]["job_id"])
        self.assertEqual(association["b_reason"], before["associations"][self.child]["reason"])
        self.assertEqual(association["method"], "llm_visual_accepted")
        self.assertEqual(association["visual_followup"]["job_id"], result["job_id"])
        for table in ("concepts", "observations", "descriptions", "assessments", "supplier_assessments",
                      "published_history", "issue_reservations"):
            self.assertEqual(after[table], before[table])

    def test_applied_exact_replay_does_not_call_model_or_write(self):
        self.first()
        before = self.memory.snapshot()
        self.assertEqual(self.first()["status"], "already_applied")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.memory.snapshot(), before)

    def test_unclear_exact_replay_keeps_deferred_without_model_or_progress_loop(self):
        self.decision = "unclear"
        self.assertEqual(self.first()["status"], "unclear")
        before = self.memory.snapshot()
        self.assertEqual(self.first()["status"], "unclear")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.memory.snapshot(), before)

    def test_different_complete_shortlist_creates_one_provisional_concept_atomically(self):
        self.decision = "different"
        result = self.first()
        self.assertEqual(result["status"], "applied")
        state = self.memory.snapshot()
        self.assertEqual(result["concept_id"], key("concept", self.child))
        self.assertEqual(state["concepts"][result["concept_id"]]["status"], "provisional")
        self.assertEqual(state["associations"][self.child]["concept_id"], result["concept_id"])

    def test_different_incomplete_shortlist_does_not_create_concept(self):
        self.add_candidates(3)
        self.images.extend([self.photo("c001", "meta001", "candidate1"), self.photo("c002", "meta002", "candidate2")])
        self.decision = "different"
        result = self.first()
        self.assertEqual(result["status"], "incomplete_comparison")
        self.assertNotIn(key("concept", self.child), self.memory.snapshot()["concepts"])
        self.first()
        self.assertEqual(len(self.calls), 1)

    def test_same_can_resolve_supported_candidate_despite_omitted_comparisons(self):
        self.add_candidates(3)
        self.assertEqual(self.first()["status"], "applied")
        self.assertEqual(self.memory.snapshot()["associations"][self.child]["concept_id"], "c000")

    def test_missing_candidate_photo_cannot_invent_different(self):
        self.add_candidates(1)
        self.decision = "different"
        result = self.first()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["applied"])
        self.assertNotIn(key("concept", self.child), self.memory.snapshot()["concepts"])

    def test_missing_all_photos_is_unavailable_without_call_or_replay_writes(self):
        self.assertEqual(self.first(images=[])["status"], "unavailable")
        before = self.memory.snapshot()
        self.first(images=[])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.memory.snapshot(), before)

    def test_photograph_owner_requires_matching_saved_source_and_association(self):
        for changes in ({"source_ref": "unknown"}, {"source_url": "https://images.example.org/invented.png"},
                        {"observed_at": "2026-10-01T10:00:00Z"}, {"owner_id": self.child}):
            images = deepcopy(self.images)
            images[1].update(changes)
            result = self.first(images=images)
            self.assertEqual(result["status"], "unavailable")
        self.assertEqual(self.calls, [])

    def test_saved_target_photo_cannot_be_relabelled_as_candidate_photo(self):
        images = deepcopy(self.images)
        images[1].update(source_ref="obs-target", source_url=images[0]["source_url"], observed_at=NOW)
        self.assertIn("OWNER_INVALID", self.first(images=images)["reason"])
        self.assertEqual(self.calls, [])

    def test_mixed_parent_image_without_unique_child_binding_is_unavailable(self):
        mixed = self.add_target("mixed", mixed=True)
        images = [self.photo(mixed, "obs-mixed", "mixed"), self.images[1]]
        self.assertEqual(self.first(images=images, child=mixed)["reason"], "VISUAL_MIXED_SOURCE_IMAGE_UNATTRIBUTED")
        self.assertEqual(self.calls, [])

    def test_b_same_is_not_genuine_unclear(self):
        not_unclear = self.add_target("not-unclear", b_decision="same")
        images = [self.photo(not_unclear, "obs-not-unclear", "not-unclear"), self.images[1]]
        self.assertEqual(self.first(images=images, child=not_unclear)["reason"], "VISUAL_GENUINE_TEXT_UNCLEAR_REQUIRED")
        self.assertEqual(self.calls, [])

    def test_invalid_saved_b_answer_is_not_genuine_unclear(self):
        association = self.memory.snapshot()["associations"][self.child]
        result_path = self.memory.root / "ai_jobs" / association["job_id"] / "result.json"
        saved = load_json(result_path)
        saved["response"]["results"][0].pop("decision")
        atomic_json(result_path, saved)
        self.assertEqual(self.first()["reason"], "VISUAL_GENUINE_TEXT_UNCLEAR_REQUIRED")
        self.assertEqual(self.calls, [])

    def test_pending_b_job_without_saved_result_is_not_visual_unclear(self):
        association = self.memory.snapshot()["associations"][self.child]
        result_path = self.memory.root / "ai_jobs" / association["job_id"] / "result.json"
        result_path.rename(result_path.with_name("fixture-withheld-result.json"))
        self.assertEqual(self.first()["reason"], "VISUAL_TEXT_RESULT_UNAVAILABLE")
        self.assertEqual(self.calls, [])

    def test_missing_b_result_is_not_turned_into_visual_work(self):
        old = self.memory.snapshot()["associations"][self.child]
        self.memory.put("associations", self.child, dict(old, job_id="missing-job"), previous=old)
        self.assertEqual(self.first()["reason"], "VISUAL_TEXT_UNCLEAR_JOB_REQUIRED")
        self.assertEqual(self.calls, [])

    def test_changed_source_replacement_cannot_use_original_photo(self):
        original = self.memory.snapshot()["observations"]["obs-target"]
        self.memory.put("observations", "replacement", dict(original, semantic_id="unrelated",
            observed_at=NOW, semantic={"title": "Different listing", "original_image_url": "https://images.example.org/different.png"}))
        # Equal-time ties resolve deterministically to the replacement ID.
        self.assertEqual(self.first()["reason"], "VISUAL_PHOTO_SOURCE_REPLACED")
        self.assertEqual(self.calls, [])

    def test_changed_candidate_during_model_does_not_apply_old_decision(self):
        def changed(role, payload, folder):
            response = self.runner(role, payload, folder)
            old = self.memory.snapshot()["concepts"]["c000"]
            self.memory.put("concepts", "c000", dict(old, description=dict(old["description"], function="Changed use")), previous=old)
            return response
        self.assertEqual(self.first(runner=changed)["status"], "inputs_changed")
        self.assertEqual(self.memory.snapshot()["associations"][self.child]["status"], "deferred")

    def test_new_source_capture_during_model_cannot_escape_initial_time_binding(self):
        def changed(role, payload, folder):
            response = self.runner(role, payload, folder)
            original = self.memory.snapshot()["observations"]["obs-target"]
            self.memory.put("observations", "new-current-source", dict(original, observed_at="2026-10-06T10:01:00+00:00"))
            return response
        self.assertEqual(self.first(runner=changed)["status"], "inputs_changed")
        self.assertEqual(self.memory.snapshot()["associations"][self.child]["status"], "deferred")

    def test_changed_a_parent_during_model_cannot_apply_to_old_child(self):
        def changed(role, payload, folder):
            response = self.runner(role, payload, folder)
            old = self.memory.snapshot()["descriptions"]["semantic-target"]
            updated = deepcopy(old)
            updated["result"]["products"][0]["function"] = "A changed product meaning"
            self.memory.put("descriptions", "semantic-target", updated, previous=old)
            return response
        self.assertEqual(self.first(runner=changed)["status"], "inputs_changed")

    def test_candidate_source_association_changed_during_model_stops_application(self):
        def changed(role, payload, folder):
            response = self.runner(role, payload, folder)
            state = self.memory.snapshot()
            association_id, old = next((i, a) for i, a in state["associations"].items() if a.get("concept_id") == "c000")
            self.memory.put("associations", association_id, dict(old, status="deferred", concept_id=None), previous=old)
            return response
        self.assertEqual(self.first(runner=changed)["status"], "inputs_changed")

    def test_changed_association_during_model_is_never_overwritten(self):
        def changed(role, payload, folder):
            response = self.runner(role, payload, folder)
            old = self.memory.snapshot()["associations"][self.child]
            self.memory.put("associations", self.child, dict(old, reason="Operator changed this question"), previous=old)
            return response
        self.assertEqual(self.first(runner=changed)["reason"], "VISUAL_ASSOCIATION_CHANGED")
        self.assertEqual(self.memory.snapshot()["associations"][self.child]["reason"], "Operator changed this question")

    def test_published_candidate_requires_review_without_history_or_fact_change(self):
        self.memory.put("published_history", "published", {"status": "published", "concept_id": "c000", "issue_id": "old"})
        before = self.memory.snapshot()
        self.assertEqual(self.first()["status"], "needs_review")
        after = self.memory.snapshot()
        for table in ("published_history", "concepts", "issue_reservations", "observations", "assessments"):
            self.assertEqual(after[table], before[table])
        self.assertIsNone(after["associations"][self.child]["concept_id"])

    def test_different_does_not_edit_compared_published_concept(self):
        self.memory.put("published_history", "published", {"status": "published", "concept_id": "c000", "issue_id": "old"})
        before = self.memory.snapshot()
        self.decision = "different"
        self.assertEqual(self.first()["status"], "applied")
        after = self.memory.snapshot()
        self.assertEqual(after["published_history"], before["published_history"])
        self.assertEqual(after["concepts"]["c000"], before["concepts"]["c000"])

    def test_existing_inactive_new_concept_key_is_not_overwritten(self):
        concept_id = key("concept", self.child)
        old = {"description": deepcopy(self.memory.snapshot()["concepts"]["c000"]["description"]), "status": "retired"}
        self.memory.put("concepts", concept_id, old)
        self.decision = "different"
        self.assertEqual(self.first()["status"], "needs_review")
        self.assertEqual(self.memory.snapshot()["concepts"][concept_id], old)

    def test_reserved_candidate_requires_review_and_replays_without_call(self):
        self.memory.put("issue_reservations", "held", {"state": "reserved", "products": [{"concept_id": "c000"}]})
        self.assertEqual(self.first()["status"], "needs_review")
        self.first()
        self.assertEqual(len(self.calls), 1)

    def test_removing_reservation_reuses_accepted_d_without_another_call(self):
        old = {"state": "reserved", "products": [{"concept_id": "c000"}]}
        self.memory.put("issue_reservations", "held", old)
        self.first()
        self.memory.put("issue_reservations", "held", dict(old, state="cancelled"), previous=old)
        self.assertEqual(self.first()["status"], "applied")
        self.assertEqual(len(self.calls), 1)

    def test_protection_added_during_model_requires_recheck(self):
        def changed(role, payload, folder):
            response = self.runner(role, payload, folder)
            self.memory.put("issue_reservations", "new-held", {"state": "reserved", "products": [{"concept_id": "c000"}]})
            return response
        self.assertEqual(self.first(runner=changed)["status"], "inputs_changed")
        self.assertEqual(self.memory.snapshot()["associations"][self.child]["status"], "deferred")

    def test_resolved_association_without_this_application_is_not_overwritten(self):
        old = self.memory.snapshot()["associations"][self.child]
        self.memory.put("associations", self.child, dict(old, status="resolved", concept_id="c000"), previous=old)
        before = self.memory.snapshot()
        self.assertEqual(self.first()["reason"], "VISUAL_DEFERRED_CHILD_REQUIRED")
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(self.calls, [])

    def test_altered_photo_bytes_fail_before_model(self):
        (self.photos / self.images[0]["path"]).write_bytes(b"invalid replacement")
        self.assertEqual(self.first()["reason"], "VISUAL_IMAGE_HASH_MISMATCH")
        self.assertEqual(self.calls, [])

    def test_one_blocked_model_item_does_not_stop_valid_sibling(self):
        other = self.add_target("other")
        other_images = [self.photo(other, "obs-other", "other"), self.images[1]]
        calls = []
        def blocked(role, payload, folder):
            calls.append(payload["items"][0]["id"])
            if payload["items"][0]["id"] == self.child:
                raise SafetyError("MODEL_ALLOWANCE_TEMPORARILY_UNAVAILABLE")
            return self.runner(role, payload, folder)
        requests = [{"child_id": self.child, "images": self.images}, {"child_id": other, "images": other_images}]
        result = resolve_saved_visual_followups(self.memory, requests, blocked, as_of=NOW)
        self.assertEqual([row["status"] for row in result["results"]], ["failed", "applied"])
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["collector_requests"], 0)
        self.assertIn(result["results"][0]["job_id"], self.memory.snapshot()["jobs"])
        resolve_saved_visual_followups(self.memory, requests, blocked, as_of=NOW)
        self.assertEqual(len(calls), 2)

    def test_failed_unchanged_question_cannot_retry_under_changed_runner_identity(self):
        def blocked(role, payload, folder):
            raise SafetyError("MODEL_ALLOWANCE_TEMPORARILY_UNAVAILABLE")
        first = self.first(runner=blocked)
        before = self.memory.snapshot()
        class ChangedRunner:
            identity = "different-runner-identity"
            def __call__(self, role, payload, folder):
                raise AssertionError("An unchanged failed question must not reach another runner.")
        result = self.first(runner=ChangedRunner())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["job_id"], first["job_id"])
        self.assertEqual(result["reason"], first["reason"])
        self.assertEqual(self.memory.snapshot(), before)

    def test_failed_unchanged_question_with_omitted_runner_keeps_original_failure(self):
        def blocked(role, payload, folder):
            raise SafetyError("MODEL_ALLOWANCE_TEMPORARILY_UNAVAILABLE")
        first = self.first(runner=blocked)
        before = self.memory.snapshot()
        result = resolve_saved_visual_followups(self.memory,
            [{"child_id": self.child, "images": self.images}], as_of=NOW)["results"][0]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["job_id"], first["job_id"])
        self.assertEqual(result["reason"], "MODEL_ALLOWANCE_TEMPORARILY_UNAVAILABLE")
        self.assertEqual(self.memory.snapshot(), before)

    def failed_network_job(self):
        def blocked(role, payload, folder):
            raise SafetyError("AI_NETWORK_UNAVAILABLE")
        result = self.first(runner=blocked)
        self.assertEqual(result["status"], "failed")
        job_id = result["job_id"]
        return job_id, self.memory.root / "ai_jobs" / job_id

    def forbidden_runner(self, role, payload, folder):
        raise AssertionError("Recovery must reuse saved results, not call the model.")

    def test_named_transient_retry_uses_exact_job_once_and_preserves_failure(self):
        job_id, base = self.failed_network_job()
        original_failure = (base / "failure.json").read_bytes()
        self.assertEqual(prepare_retry(self.memory, job_id, "Fixture network repaired."), 1)
        result = self.first()
        self.assertEqual((result["status"], result["job_id"]), ("applied", job_id))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((base / "failure.json").read_bytes(), original_failure)
        self.assertTrue((base / "retry-1" / "result.json").is_file())
        self.assertEqual(self.memory.snapshot()["associations"][self.child]["visual_followup"]["job_attempt"], "retry-1")
        self.assertEqual(self.first()["status"], "already_applied")
        self.assertEqual(len(self.calls), 1)

    def test_failed_retry_does_not_automatically_start_third_call(self):
        job_id, base = self.failed_network_job()
        prepare_retry(self.memory, job_id, "Fixture deliberate retry.")
        attempts = []
        def blocked(role, payload, folder):
            attempts.append(folder.name)
            raise SafetyError("AI_NETWORK_UNAVAILABLE")
        self.assertEqual(self.first(runner=blocked)["status"], "failed")
        before = self.memory.snapshot()
        self.assertEqual(self.first(runner=blocked)["status"], "failed")
        self.assertEqual(attempts, ["retry-1"])
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(len(load_json(base / "retries.json")), 1)

    def test_original_saved_result_reconciles_without_model(self):
        job_id, base = self.failed_network_job()
        request = self.memory.snapshot()["jobs"][job_id]["request"]
        response, usage = self.runner("D", request["payload"], base)
        self.calls.clear()
        atomic_json(base / "result.json", {"request_hash": digest(request), "response": response, "usage": usage})
        result = self.first(runner=self.forbidden_runner)
        self.assertEqual((result["status"], result["job_id"]), ("applied", job_id))
        self.assertEqual(self.calls, [])

    def test_original_saved_completion_reconciles_without_model(self):
        job_id, base = self.failed_network_job()
        request = self.memory.snapshot()["jobs"][job_id]["request"]
        response, usage = self.runner("D", request["payload"], base)
        self.calls.clear()
        atomic_json(base / "completion.json", {"response": response, "usage": usage})
        result = self.first(runner=self.forbidden_runner)
        self.assertEqual((result["status"], result["job_id"]), ("applied", job_id))
        self.assertEqual(load_json(base / "result.json")["request_hash"], digest(request))
        self.assertTrue((base / "failure.json").exists())
        self.assertEqual(self.calls, [])

    def test_invalid_saved_result_cannot_reopen_failure_or_call_model(self):
        job_id, base = self.failed_network_job()
        request = self.memory.snapshot()["jobs"][job_id]["request"]
        atomic_json(base / "result.json", {"request_hash": digest(request), "response": {"results": []}})
        before = self.memory.snapshot()
        result = self.first(runner=self.forbidden_runner)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.memory.snapshot(), before)

    def test_conflicting_saved_result_binding_cannot_apply_or_call_model(self):
        _, base = self.failed_network_job()
        atomic_json(base / "result.json", {"request_hash": "wrong", "response": {"results": []}})
        before = self.memory.snapshot()
        result = self.first(runner=self.forbidden_runner)
        self.assertEqual(result["reason"], "AI_JOB_RESULT_CONFLICT")
        self.assertEqual(self.memory.snapshot(), before)

    def test_malformed_saved_result_stays_contained_without_model(self):
        _, base = self.failed_network_job()
        atomic_json(base / "result.json", [])
        before = self.memory.snapshot()
        result = self.first(runner=self.forbidden_runner)
        self.assertEqual(result["reason"], "AI_JOB_RESULT_CONFLICT")
        self.assertEqual(self.memory.snapshot(), before)

    def test_conflicting_saved_completion_intent_cannot_apply_or_call_model(self):
        job_id, base = self.failed_network_job()
        request = self.memory.snapshot()["jobs"][job_id]["request"]
        response, usage = self.runner("D", request["payload"], base)
        atomic_json(base / "completion.json", {"response": response, "usage": usage})
        atomic_json(base / "intent.json", {"request": dict(request, runner="changed")})
        before = self.memory.snapshot()
        result = self.first(runner=self.forbidden_runner)
        self.assertEqual(result["reason"], "AI_JOB_RESULT_CONFLICT")
        self.assertEqual(self.memory.snapshot(), before)
        self.assertFalse((base / "result.json").exists())

    def test_explicit_retry_with_changed_runner_does_not_create_another_job(self):
        job_id, _ = self.failed_network_job()
        prepare_retry(self.memory, job_id, "Fixture network repaired.")
        class ChangedRunner:
            identity = "changed-runner"
            def __call__(self, role, payload, folder):
                raise AssertionError("Retry belongs to the original runner identity.")
        before = self.memory.snapshot()
        result = self.first(runner=ChangedRunner())
        self.assertEqual((result["status"], result["job_id"]), ("failed", job_id))
        self.assertEqual(self.memory.snapshot(), before)

    def test_pending_explicit_retry_does_not_call_again(self):
        job_id, base = self.failed_network_job()
        prepare_retry(self.memory, job_id, "Fixture network repaired.")
        request = self.memory.snapshot()["jobs"][job_id]["request"]
        atomic_json(base / "retry-1" / "intent.json", {"request": request})
        before = self.memory.snapshot()
        self.assertEqual(self.first(runner=self.forbidden_runner)["status"], "failed")
        self.assertEqual(self.memory.snapshot(), before)

    def test_no_model_inspection_does_not_consume_prepared_retry(self):
        job_id, base = self.failed_network_job()
        prepare_retry(self.memory, job_id, "Fixture network repaired.")
        before = self.memory.snapshot()
        result = resolve_saved_visual_followups(self.memory,
            [{"child_id": self.child, "images": self.images}], as_of=NOW)["results"][0]
        self.assertEqual(result["reason"], "AI_NETWORK_UNAVAILABLE")
        self.assertEqual(self.memory.snapshot(), before)
        self.assertFalse((base / "retry-1" / "intent.json").exists())
        self.assertEqual(self.first()["status"], "applied")
        self.assertEqual(len(self.calls), 1)

    def test_retry_cannot_reopen_if_original_failure_proof_changed(self):
        job_id, base = self.failed_network_job()
        prepare_retry(self.memory, job_id, "Fixture network repaired.")
        atomic_json(base / "failure.json", {"code": "different failure"})
        before = self.memory.snapshot()
        self.assertEqual(self.first(runner=self.forbidden_runner)["status"], "failed")
        self.assertEqual(self.memory.snapshot(), before)

    def test_second_deliberate_retry_can_recover_legacy_attempt_record(self):
        job_id, base = self.failed_network_job()
        prepare_retry(self.memory, job_id, "Fixture first repair.")
        def blocked(role, payload, folder):
            raise SafetyError("AI_NETWORK_UNAVAILABLE")
        self.assertEqual(self.first(runner=blocked)["status"], "failed")
        previous = self.memory.snapshot()["associations"][self.child]
        legacy = deepcopy(previous)
        legacy["visual_followup"].pop("job_attempt")
        self.memory.put("associations", self.child, legacy, previous=previous)
        prepare_retry(self.memory, job_id, "Fixture second repair.")
        self.assertEqual(self.first()["status"], "applied")
        self.assertEqual(len(self.calls), 1)
        self.assertTrue((base / "retry-2" / "result.json").exists())

    def test_saved_completion_recovery_still_checks_report_protection(self):
        self.memory.put("issue_reservations", "held", {"state": "reserved", "products": [{"concept_id": "c000"}]})
        job_id, base = self.failed_network_job()
        request = self.memory.snapshot()["jobs"][job_id]["request"]
        response, usage = self.runner("D", request["payload"], base)
        self.calls.clear()
        atomic_json(base / "completion.json", {"response": response, "usage": usage})
        before = self.memory.snapshot()
        self.assertEqual(self.first(runner=self.forbidden_runner)["status"], "needs_review")
        after = self.memory.snapshot()
        self.assertEqual(after["associations"][self.child]["status"], "deferred")
        self.assertEqual(after["issue_reservations"], before["issue_reservations"])
        self.assertEqual(after["concepts"], before["concepts"])
        self.assertEqual(self.calls, [])

    def test_invalid_request_limits_or_duplicates_are_rejected(self):
        for requests in ([], [{}], [{"child_id": self.child, "images": []}] * 2,
                         [{"child_id": "child" + str(n), "images": []} for n in range(6)]):
            with self.assertRaises(SafetyError):
                resolve_saved_visual_followups(self.memory, requests, self.runner, as_of=NOW)


if __name__ == "__main__":
    unittest.main()
