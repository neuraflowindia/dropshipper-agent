"""Source-bound operator correction fixtures; not semantic model guarantees."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.identity_corrections import apply_unallocated_identity_merge
from product_radar.models import SafetyError, digest
from product_radar.providers.apify import map_meta_rows
from product_radar.research_flow import assessment_item, candidate_summary
from product_radar.research_memory import ResearchMemory, change, import_capture, key
from product_radar.report_selection import _assessment
from product_radar.storage import Journal


NOW = "2026-10-05T10:00:00+00:00"
OBSERVED = "2026-10-04T10:00:00+00:00"


def product(label):
    return {"label": label, "category": "Home and Decor", "function": "Hold books upright",
            "mechanism": "Fixed side supports", "audience": "People arranging books",
            "features": [label], "basis": "observed", "reason": "Synthetic observed theme.",
            "brand": "generic_or_small"}


class IdentityCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        values = [{"ad_archive_id": str(100 + n), "page_id": str(200 + n), "is_active": True,
                   "start_date": 1788220800, "snapshot": {"title": label}}
                  for n, label in enumerate(("Bear bookends", "Bird bookends", "Cat bookends", "Other product"))]
        import_capture(self.memory, source="meta", acquisition_id="fixture1", observed_at=OBSERVED,
                       mapped=map_meta_rows(values, purpose="discovery", query_reference="fixture"),
                       capture_hash=digest(values), reference={"fixture": "synthetic"})
        self.observations, self.children = {}, {}
        for index, (observation_id, observation) in enumerate(self.memory.snapshot()["observations"].items()):
            concept_id = ("bear", "bird", "cat", "other")[index]
            description = product(observation["semantic"]["title"])
            if concept_id == "other":
                description.update(function="Clean surfaces", mechanism="powered")
            result = {"id": observation["semantic_id"], "kind": "product", "products": [description], "reason": "Fixture."}
            child_id = key("child", [observation["semantic_id"], 0, digest(result)])
            self.memory.commit("seed-" + concept_id, [
                change("descriptions", observation["semantic_id"], {"result": result, "job_id": "a-" + concept_id}),
                change("concepts", concept_id, {"description": description, "status": "provisional"}),
                change("associations", child_id, {"semantic_id": observation["semantic_id"], "concept_id": concept_id,
                       "status": "resolved", "reason": "Original B different decision.", "job_id": "b-" + concept_id,
                       "method": "llm_accepted"}),
                change("jobs", "a-" + concept_id, {"status": "complete", "fixture": "A"}),
                change("jobs", "b-" + concept_id, {"status": "complete", "fixture": "B"})])
            self.observations[concept_id] = observation_id
            self.children[concept_id] = child_id
        state = self.memory.snapshot()
        for concept_id in ("bear", "bird", "cat", "other"):
            observation_id = self.observations[concept_id]
            result = {"id": concept_id, "criteria": [
                {"id": n, "state": "Not assessed" if n == 3 else "Meets", "basis": "inferred",
                 "references": [observation_id], "reason": "Synthetic benefit judgment."}
                for n in range(1, 14)], "relevance": [
                    {"id": observation_id, "relationship": "exact_product", "reason": "Fixture."}],
                "compliance_note": "", "missing_questions": []}
            self.memory.put("assessments", concept_id, {"input_hash": digest(assessment_item(state, concept_id)),
                            "result": result, "job_id": "e-" + concept_id, "used_record_ids": [observation_id]})
        self.memory.put("supplier_assessments", "supplier-bird", {"fixture": "Original F", "concept_id": "bird"})
        self.memory.put("trends_evidence", "trends-bird", {"fixture": "Original chart", "concept_id": "bird"})
        self.request = self.make_request()

    def make_request(self, members=("bear", "bird", "cat")):
        state = self.memory.snapshot()
        return {"survivor_id": "bear", "concept_hashes": {i: digest(state["concepts"][i]) for i in members},
                "association_hashes": {i: digest(row) for i, row in state["associations"].items()
                                       if row.get("status") == "resolved" and row["concept_id"] in members},
                "evidence_ids": {i: [self.observations[i]] for i in members},
                "author": "fixture_operator", "reason": "These decorative themes have the same fixed-support function and mechanism.",
                "reviewed_at": NOW}

    def update(self, table, record_id, **values):
        old = self.memory.snapshot()[table][record_id]
        self.memory.put(table, record_id, dict(old, **values), previous=old)

    def refuse(self, code, request=None):
        before = self.memory.snapshot()
        events = list((self.memory.root / "events").glob("*.json"))
        with self.assertRaisesRegex(SafetyError, code):
            apply_unallocated_identity_merge(self.memory, request if request is not None else self.request)
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(list((self.memory.root / "events").glob("*.json")), events)

    def test_merge_is_one_event_preserves_original_research_and_association_provenance(self):
        before = self.memory.snapshot()
        count = len(list((self.memory.root / "events").glob("*.json")))
        outcome = apply_unallocated_identity_merge(self.memory, self.request)
        after = self.memory.snapshot()
        self.assertTrue(outcome["applied"])
        self.assertEqual(outcome["merged_ids"], ["bird", "cat"])
        self.assertEqual(len(list((self.memory.root / "events").glob("*.json"))), count + 1)
        for name in ("captures", "observations", "descriptions", "jobs", "assessments", "supplier_assessments", "trends_evidence",
                     "problems", "published_history", "issue_reservations"):
            self.assertEqual(after[name], before[name], name)
        self.assertEqual(after["concepts"]["other"], before["concepts"]["other"])
        self.assertEqual(after["associations"][self.children["bear"]], before["associations"][self.children["bear"]])
        for member in ("bird", "cat"):
            self.assertEqual(after["concepts"][member]["merged_into"], "bear")
            self.assertEqual(after["concepts"][member]["status"], "merged")
            link = after["associations"][self.children[member]]
            self.assertEqual(link["concept_id"], "bear")
            self.assertEqual(link["job_id"], "b-" + member)
            self.assertEqual(link["identity_correction"]["previous_concept_id"], member)
            self.assertEqual(link["identity_correction"]["previous_reason"], "Original B different decision.")
        review = after["concepts"]["bear"]["identity_corrections"][outcome["review_id"]]
        self.assertEqual(review["request"], self.request)
        self.assertEqual(review["method"], "operator_source_review")

    def test_exact_replay_is_no_event_and_no_mutation(self):
        first = apply_unallocated_identity_merge(self.memory, self.request)
        before = self.memory.snapshot()
        count = len(list((self.memory.root / "events").glob("*.json")))
        second = apply_unallocated_identity_merge(self.memory, deepcopy(self.request))
        self.assertFalse(second["applied"])
        self.assertEqual(first["review_id"], second["review_id"])
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(len(list((self.memory.root / "events").glob("*.json"))), count)

    def test_stale_concept_hash_refuses(self):
        self.update("concepts", "bird", note="Changed after review")
        self.refuse("IDENTITY_MERGE_INPUT_CHANGED")

    def test_stale_association_hash_refuses(self):
        self.update("associations", self.children["bird"], reason="Changed after review")
        self.refuse("IDENTITY_MERGE_INPUT_CHANGED")

    def test_omitted_association_refuses_even_survivor_link(self):
        self.request["association_hashes"].pop(self.children["bear"])
        self.refuse("IDENTITY_MERGE_INPUT_CHANGED")

    def test_new_association_refuses(self):
        old = self.memory.snapshot()["associations"][self.children["other"]]
        self.memory.put("associations", self.children["other"], dict(old, concept_id="bird"), previous=old)
        self.refuse("IDENTITY_MERGE_INPUT_CHANGED")

    def test_replay_refuses_new_association(self):
        apply_unallocated_identity_merge(self.memory, self.request)
        old = self.memory.snapshot()["associations"][self.children["other"]]
        self.memory.put("associations", self.children["other"], dict(old, concept_id="bear"), previous=old)
        self.refuse("IDENTITY_MERGE_INPUT_CHANGED")

    def test_replay_refuses_changed_survivor_or_duplicate(self):
        apply_unallocated_identity_merge(self.memory, self.request)
        for member in ("bear", "bird"):
            with self.subTest(member=member):
                self.update("concepts", member, note="Subsequent meaning review")
                self.refuse("IDENTITY_MERGE_INPUT_CHANGED")

    def test_current_accepted_child_description_required(self):
        semantic_id = self.memory.snapshot()["associations"][self.children["bird"]]["semantic_id"]
        old = self.memory.snapshot()["descriptions"][semantic_id]
        result = deepcopy(old["result"])
        result["products"][0]["features"] = ["New mechanical meaning"]
        self.update("descriptions", semantic_id, result=result)
        self.refuse("IDENTITY_MERGE_DESCRIPTION_CHANGED")

    def test_replay_refuses_changed_description_metadata(self):
        apply_unallocated_identity_merge(self.memory, self.request)
        semantic_id = self.memory.snapshot()["associations"][self.children["bird"]]["semantic_id"]
        self.update("descriptions", semantic_id, job_id="replacement-a")
        self.refuse("IDENTITY_MERGE_DESCRIPTION_CHANGED")

    def test_wrong_member_or_unknown_source_refuses(self):
        for observation_id in (self.observations["bear"], "missing-observation"):
            with self.subTest(observation_id=observation_id):
                request = deepcopy(self.request)
                request["evidence_ids"]["bird"] = [observation_id]
                self.refuse("IDENTITY_MERGE_SOURCE_INVALID", request)

    def test_future_review_refuses(self):
        request = dict(self.request, reviewed_at="2099-01-01T00:00:00+00:00")
        self.refuse("IDENTITY_MERGE_REVIEW_IN_FUTURE", request)

    def test_source_observed_after_operator_review_refuses(self):
        request = dict(self.request, reviewed_at="2026-10-03T10:00:00+00:00")
        self.refuse("IDENTITY_MERGE_SOURCE_INVALID", request)

    def test_published_history_for_any_client_protects_group(self):
        self.memory.put("published_history", "other-client-history", {"status": "published", "client_id": "another_client", "concept_id": "cat"})
        self.refuse("IDENTITY_MERGE_RELEASE_REVIEW_REQUIRED")

    def test_active_reservation_states_for_any_client_protect_group(self):
        for state in ("reserved", "uploaded", "published"):
            with self.subTest(state=state):
                self.memory.put("issue_reservations", "issue-" + state, {"state": state, "client_id": "another_client", "products": [{"concept_id": "bird"}]})
                self.refuse("IDENTITY_MERGE_RELEASE_REVIEW_REQUIRED")

    def test_protection_resolves_incoming_existing_redirects(self):
        self.memory.put("concepts", "old-bear", {"description": product("Old theme"), "status": "merged", "merged_into": "bear"})
        self.memory.put("published_history", "old-history", {"status": "published", "concept_id": "old-bear"})
        self.refuse("IDENTITY_MERGE_RELEASE_REVIEW_REQUIRED")

    def test_unrelated_active_or_cancelled_allocations_do_not_block(self):
        self.memory.put("issue_reservations", "other-report", {"state": "uploaded", "products": [{"concept_id": "other"}]})
        self.memory.put("issue_reservations", "cancelled-report", {"state": "cancelled", "products": [{"concept_id": "bird"}]})
        self.assertTrue(apply_unallocated_identity_merge(self.memory, self.request)["applied"])

    def test_explicit_observed_mechanism_conflict_refuses_not_theme_words(self):
        old = self.memory.snapshot()["concepts"]["bear"]
        self.update("concepts", "bear", description=dict(old["description"], mechanism="manual"))
        old = self.memory.snapshot()["concepts"]["bird"]
        self.update("concepts", "bird", description=dict(old["description"], mechanism="powered"))
        self.request = self.make_request()
        self.refuse("IDENTITY_MERGE_KNOWN_CONFLICT")

    def test_category_conflict_refuses(self):
        old = self.memory.snapshot()["concepts"]["bird"]
        self.update("concepts", "bird", description=dict(old["description"], category="Travel and Outdoor"))
        self.request = self.make_request()
        self.refuse("IDENTITY_MERGE_CATEGORY_CONFLICT")

    def test_excluded_conflicted_established_or_retired_member_refuses(self):
        for updates in ({"status": "excluded"}, {"status": "retired"}, {"identity_conflicted": True},
                        {"description": dict(product("Bird"), brand="established")}):
            with self.subTest(updates=updates):
                self.update("concepts", "bird", **updates)
                self.request = self.make_request()
                self.refuse("IDENTITY_MERGE_CONCEPT_EXCLUDED")

    def test_atomic_commit_failure_preserves_all_records(self):
        before = self.memory.snapshot()
        with patch.object(self.memory, "commit", side_effect=SafetyError("FIXTURE_COMMIT_FAILURE")):
            self.refuse("FIXTURE_COMMIT_FAILURE")
        self.assertEqual(self.memory.snapshot(), before)

    def test_actual_multi_change_commit_dependency_failure_is_atomic(self):
        original_commit = self.memory.commit
        def fail_last_dependency(event_id, changes, *, validate=None):
            changes = deepcopy(changes)
            changes[-1]["previous_hash"] = "0" * 64
            return original_commit(event_id, changes, validate=validate)
        with patch.object(self.memory, "commit", side_effect=fail_last_dependency):
            self.refuse("RESEARCH_DEPENDENCY_CHANGED")

    def test_completed_exact_replay_does_not_rewrite_subsequent_report_history(self):
        apply_unallocated_identity_merge(self.memory, self.request)
        self.memory.put("published_history", "later-report", {"status": "published", "concept_id": "bear"})
        before = self.memory.snapshot()
        self.assertFalse(apply_unallocated_identity_merge(self.memory, self.request)["applied"])
        self.assertEqual(self.memory.snapshot(), before)

    def test_survivor_existing_empty_review_field_replays(self):
        self.update("concepts", "bear", identity_corrections={})
        self.request = self.make_request()
        apply_unallocated_identity_merge(self.memory, self.request)
        self.assertFalse(apply_unallocated_identity_merge(self.memory, self.request)["applied"])

    def test_merge_invalidates_current_checklist_without_copying_duplicate_score(self):
        before = self.memory.snapshot()
        apply_unallocated_identity_merge(self.memory, self.request)
        after = self.memory.snapshot()
        self.assertNotEqual(after["assessments"]["bear"]["input_hash"], digest(assessment_item(after, "bear")))
        self.assertEqual(after["assessments"], before["assessments"])
        from product_radar.research_flow import evidence_indexes
        by_concept, by_semantic = evidence_indexes(after)
        record, code, _ = _assessment(after, "bear", after["concepts"]["bear"], NOW, by_concept, by_semantic)
        self.assertIsNone(record)
        self.assertEqual(code, "checklist_review_required")
        summary = candidate_summary(self.memory, as_of=NOW)
        self.assertEqual({row["id"] for row in summary["products"]}, {"bear", "other"})
        self.assertIsNone(next(row for row in summary["products"] if row["id"] == "bear")["checklist_meets"])

    def test_operator_generic_description_is_bound_and_replays(self):
        generic = product("Decorative fixed bookends")
        generic["features"] = ["Fixed decorative side supports"]
        request = dict(self.request, survivor_description=generic)
        result = apply_unallocated_identity_merge(self.memory, request)
        state = self.memory.snapshot()
        self.assertEqual(state["concepts"]["bear"]["description"], generic)
        self.assertEqual(state["descriptions"][next(iter(state["descriptions"]))]["result"]["products"][0]["label"], "Bear bookends")
        self.assertFalse(apply_unallocated_identity_merge(self.memory, request)["applied"])
        changed = deepcopy(request)
        changed["survivor_description"]["features"] = ["Invented extra feature"]
        self.refuse("IDENTITY_MERGE_INPUT_CHANGED", changed)
        self.assertTrue(result["reassessment_required"])

    def test_optional_description_conflict_and_empty_meaning_refuse(self):
        for values in ({"category": "Travel and Outdoor"}, {"function": ""}, {"brand": "established"}):
            with self.subTest(values=values):
                request = dict(self.request, survivor_description=dict(product("Generic bookends"), **values))
                self.refuse("IDENTITY_MERGE_CATEGORY_CONFLICT|AI_TEXT_INVALID|IDENTITY_MERGE_CONCEPT_EXCLUDED", request)

    def test_bound_group_and_schema_refuse_invalid_request(self):
        for update in ({"concept_hashes": {"bear": "0" * 64}}, {"author": ""}, {"unexpected": True},
                       {"concept_hashes": {str(i): "0" * 64 for i in range(21)}},
                       {"association_hashes": {}}, {"evidence_ids": {"bear": [], "bird": [], "cat": []}}):
            with self.subTest(update=update):
                self.refuse("IDENTITY_MERGE_REQUEST_INVALID|IDENTITY_MERGE_SOURCE_INVALID|AI_TEXT_INVALID", dict(self.request, **update))

    def test_no_model_or_financial_event(self):
        before = self.memory.journal.path.read_bytes() if self.memory.journal.path.exists() else None
        with patch("product_radar.research_ai.Jobs.run", side_effect=AssertionError("No model permitted")) as run:
            apply_unallocated_identity_merge(self.memory, self.request)
            run.assert_not_called()
        after = self.memory.journal.path.read_bytes() if self.memory.journal.path.exists() else None
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
