"""Exact empty-query refinement in isolated fixtures; no real research writes."""

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_research_planner import catalogue, outcome, NOW
from test_research_flow import rows
from test_meta_live_trial import CONFIG, cli
from product_radar.catalogue import load_catalogue, catalogue_summary
from product_radar.models import SafetyError, canonical, digest, prepared_request
from product_radar.providers.apify import map_meta_rows
from product_radar.query_dispatch import find_existing_task
from product_radar.research_memory import ResearchMemory, import_capture
from product_radar.research_operations import bind_prepared_action, plan_research, record_query_outcome, sync_query_feedback
from product_radar.research_planner import plan_searches, semantic_key, validate_outcome
from product_radar.storage import Journal, atomic_bytes, atomic_json, load_json


class EmptyRefinementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.data = catalogue()
        family = self.data["families"][0]
        family["phrases"][0]["text"] = "adjustable fixture bookends"
        family["phrases"].append(dict(family["phrases"][0], text="fixture bookends", reason="A reviewed common-name alternative."))
        self.original = plan_research(self.memory, self.data, as_of=NOW, slots=1)
        self.action = self.original["actions"][0]
        self.prepared = self.prepare(self.action, "fixture-original")
        self.task = bind_prepared_action(self.memory, self.original["id"], self.action["action_id"], self.prepared)
        self.attempts = {}

    def prepare(self, action, attempt_id):
        config = deepcopy(CONFIG)
        config.update(term=action["phrase"], purpose="discovery")
        return prepared_request(cli.prepare(config, attempt_id)["request"])

    def financial(self):
        return patch.object(self.memory.journal, "state", return_value=({}, deepcopy(self.attempts)))

    def commit(self, task=None, prepared=None, *, provider_items=1, values=None):
        task, prepared = task or self.task, prepared or self.prepared
        url = prepared.input["urls"][0]["url"]
        values = [{"error": "Ads not found", "errorCode": "ADS_NOT_FOUND", "url": url}] if values is None else values
        raw = canonical(values)
        dataset = "dataset-" + prepared.attempt_id
        folder = self.memory.journal.root / prepared.attempt_id
        reference = "http-fixture"
        meta = {"id": reference, "method": "GET", "path": "/v2/datasets/" + dataset + "/items", "status": 200,
                "query": {"format": "json", "offset": 0, "limit": 100}, "bytes": len(raw),
                "hash": hashlib.sha256(raw).hexdigest(), "headers": {
                    "x-apify-pagination-" + field: str(value) for field, value in
                    {"offset": 0, "limit": 100, "count": provider_items, "total": provider_items}.items()}}
        manifest = {"captured_hash": digest(values), "dataset_id": dataset, "observed_at": NOW,
                    "bounded_capture_complete": True, "truncated": False, "collection_status": "SUCCEEDED",
                    "purpose": prepared.purpose,
                    "captured_count": 1, "returned_total": provider_items,
                    "pages": [{"offset": 0, "count": 1, "total": provider_items, "response": reference}]}
        if provider_items == 0:
            manifest["capture_contents_kind"] = "source_error_diagnostic_not_dataset_items"
        atomic_json(folder / "prepared.json", prepared.to_dict())
        atomic_json(folder / "captured_rows.json", values)
        atomic_json(folder / "capture_manifest.json", manifest)
        atomic_json(folder / "http" / (reference + ".json"), meta)
        atomic_bytes(folder / "http" / (reference + ".body.json"), raw)
        self.attempts[prepared.attempt_id] = {"submission": "submitted", "remote_status": "SUCCEEDED", "charge": "0.01",
            "read_charge": None, "capture": digest(values), "capture_status": "captured", "dataset_id": dataset,
            "controls_ok": True, "config_hash": prepared.config_hash}
        import_capture(self.memory, source="meta", acquisition_id=dataset, observed_at=NOW,
            mapped=map_meta_rows(values, purpose="discovery", query_reference=prepared.attempt_id),
            capture_hash=digest(values), reference={"attempt_id": prepared.attempt_id})
        return folder

    def feedback(self):
        with self.financial():
            sync_query_feedback(self.memory, as_of=NOW)
        return next(row for row in self.memory.snapshot()["query_outcomes"].values()
                    if row["acquisition_id"] == "dataset-fixture-original")

    def refine(self, **options):
        return plan_research(self.memory, self.data, as_of=NOW, slots=1,
                             urgent_family_ids=[self.action["family_id"]], **options)

    def test_verified_zero_counter_diagnostic_preserves_zero_ads(self):
        self.commit(provider_items=0)
        measured = self.feedback()
        self.assertEqual(measured["empty_result"]["provider_items"], 0)
        self.assertEqual(measured["empty_result"]["ad_rows"], 0)
        self.assertEqual(measured["empty_result"]["request_key"], semantic_key(self.action["request"]))
        self.assertTrue(measured["complete"] and measured["reviewed"])

    def test_one_counted_diagnostic_is_not_reported_as_zero_provider_items(self):
        self.commit(provider_items=1)
        self.assertEqual(self.feedback()["empty_result"]["provider_items"], 1)

    def test_proven_empty_diagnostic_can_complete_a_mapper_source_error_classification(self):
        self.commit()
        capture_id, saved = next(iter(self.memory.snapshot()["captures"].items()))
        # Exercise the feedback transition without rewriting the immutable
        # capture: the supplied saved-data view has a mapper-level error label.
        snapshot = self.memory.snapshot()
        snapshot["captures"][capture_id]["mapping_status"] = "source_failure"
        with self.financial(), patch.object(self.memory, "snapshot", return_value=snapshot), patch(
                "product_radar.research_operations.record_query_outcome", side_effect=lambda m, value: validate_outcome(value)):
            measured = sync_query_feedback(self.memory, as_of=NOW)[0]
        self.assertEqual(measured["status"], "completed")
        self.assertTrue(measured["complete"] and measured["reviewed"])
        self.assertEqual(measured["empty_result"]["ad_rows"], 0)
        self.assertEqual(self.memory.snapshot()["query_tasks"][self.task["id"]]["status"], "completed")

    def test_empty_primary_chooses_one_reviewed_same_family_alternative(self):
        self.commit(); base = self.feedback()
        plan = self.refine(budget_usd="0.11")
        chosen = plan["actions"][0]
        self.assertEqual(chosen["phrase"], "fixture bookends")
        self.assertEqual(chosen["kind"], "collect")
        self.assertEqual(chosen["family_id"], base["family_id"])
        self.assertEqual(chosen["refinement"]["base_outcome_id"], base["id"])
        self.assertEqual(chosen["refinement"]["base_capture_hash"], base["empty_result"]["capture_hash"])
        self.assertEqual(chosen["request"]["purpose"], "discovery")
        self.assertEqual(chosen["request"]["coverage"], {"limit": 5})
        self.assertEqual(plan, self.refine(budget_usd="0.11"))
        self.assertEqual(self.memory.snapshot()["batch_plans"][self.original["id"]], self.original)

    def test_ordinary_refinement_is_fourth_after_three_untouched_areas(self):
        self.commit(); self.feedback()
        plan = plan_research(self.memory, self.data, as_of=NOW, slots=4, budget_usd="0.26")
        self.assertTrue(all(a["family_id"] != self.action["family_id"] for a in plan["actions"][:3]))
        self.assertEqual(plan["actions"][3]["phrase"], "fixture bookends")
        self.assertIn("not an unexplored", plan["actions"][3]["reason"])

    def test_mixed_urgent_new_families_and_empty_query_refinement_are_ordered(self):
        self.commit(provider_items=0)
        base = self.feedback()
        plan = plan_research(self.memory, self.data, as_of=NOW, slots=3, budget_usd="0.25",
                             urgent_family_ids=[self.action["family_id"], "f1-0", "f2-0"])
        self.assertEqual(len(plan["actions"]), 3)
        self.assertEqual({a["family_id"] for a in plan["actions"][:2]}, {"f1-0", "f2-0"})
        last = plan["actions"][2]
        self.assertEqual(last["phrase"], "fixture bookends")
        self.assertEqual(last["refinement"]["base_outcome_id"], base["id"])
        self.assertEqual(last["refinement"]["base_capture_hash"], base["empty_result"]["capture_hash"])
        self.assertEqual(self.memory.snapshot()["batch_plans"][self.original["id"]], self.original)

    def test_no_alternative_or_no_budget_does_not_reuse_empty_capture(self):
        self.commit(); self.feedback()
        self.assertEqual(self.refine(budget_usd="0.06")["actions"], [])
        self.data["families"][0]["phrases"].pop()
        plan = self.refine()
        self.assertTrue(all(a["family_id"] != self.action["family_id"] for a in plan["actions"]))
        self.assertIn("empty-ad diagnostic", str(plan["decisions"]))

    def test_pending_saved_refinement_is_not_replaced_by_another_phrase(self):
        self.commit(); self.feedback()
        plan = self.refine()
        chosen = plan["actions"][0]
        prepared = self.prepare(chosen, "fixture-alternate")
        task = bind_prepared_action(self.memory, plan["id"], chosen["action_id"], prepared)
        self.assertEqual(task["refinement"], chosen["refinement"])
        self.data["families"][0]["phrases"].append(dict(self.data["families"][0]["phrases"][0], text="book shelf holders"))
        self.assertTrue(all(a["family_id"] != chosen["family_id"] for a in self.refine()["actions"]))
        with self.financial():
            existing = find_existing_task(self.memory, chosen, as_of=NOW)
        self.assertEqual(existing["reuse_kind"], "active_task")
        self.assertEqual(existing["attempt_id"], prepared.attempt_id)
        self.assertEqual(task, bind_prepared_action(self.memory, plan["id"], chosen["action_id"], prepared))

    def test_pending_alternative_blocks_original_replacement_after_cache_expiry(self):
        self.commit(); self.feedback()
        plan = self.refine(); chosen = plan["actions"][0]
        prepared = self.prepare(chosen, "fixture-alternate")
        bind_prepared_action(self.memory, plan["id"], chosen["action_id"], prepared)
        later = (datetime.fromisoformat(NOW) + timedelta(days=8)).isoformat()
        aged = plan_research(self.memory, self.data, as_of=later, slots=12,
                             urgent_family_ids=[chosen["family_id"]])
        self.assertTrue(all(a["family_id"] != chosen["family_id"] for a in aged["actions"]))
        self.assertIn("original capture is old", str(aged["decisions"]))
        with self.financial():
            self.assertEqual(find_existing_task(self.memory, chosen, as_of=later)["attempt_id"], prepared.attempt_id)

    def test_empty_alternative_does_not_start_a_chain(self):
        self.commit(); self.feedback()
        plan = self.refine(); chosen = plan["actions"][0]
        prepared = self.prepare(chosen, "fixture-alternate")
        task = bind_prepared_action(self.memory, plan["id"], chosen["action_id"], prepared)
        self.commit(task, prepared)
        with self.financial():
            sync_query_feedback(self.memory, as_of=NOW)
        alternate = next(row for row in self.memory.snapshot()["query_outcomes"].values() if "refinement" in row)
        self.assertIn("empty_result", alternate)
        self.data["families"][0]["phrases"].append(dict(self.data["families"][0]["phrases"][0], text="book shelf holders"))
        plan = self.refine()
        self.assertTrue(all(a["family_id"] != chosen["family_id"] for a in plan["actions"]))
        self.assertIn("one refinement", str(plan["decisions"]))

    def test_nonempty_alternative_reuses_its_exact_capture_and_original_dates(self):
        self.commit(); self.feedback()
        plan = self.refine(); chosen = plan["actions"][0]
        prepared = self.prepare(chosen, "fixture-alternate")
        task = bind_prepared_action(self.memory, plan["id"], chosen["action_id"], prepared)
        self.commit(task, prepared, values=[rows()[0]])
        with self.financial():
            sync_query_feedback(self.memory, as_of=NOW)
        replay = self.refine()["actions"][0]
        self.assertEqual(replay["phrase"], chosen["phrase"])
        self.assertEqual(replay["kind"], "reuse")
        self.assertEqual(replay["cost_estimate_usd"], "0")
        self.assertEqual(replay["captured_observed_at"], NOW)
        self.assertEqual(replay["refinement"], chosen["refinement"])
        with self.financial():
            self.assertEqual(find_existing_task(self.memory, replay, as_of=NOW)["reuse_kind"], "fresh_capture")

    def test_failed_alternative_retains_failure_without_recollecting(self):
        self.commit(); self.feedback()
        plan = self.refine(); chosen = plan["actions"][0]
        prepared = self.prepare(chosen, "fixture-alternate")
        task = bind_prepared_action(self.memory, plan["id"], chosen["action_id"], prepared)
        self.commit(task, prepared, values=[{"error": "Source unavailable"}])
        self.attempts[prepared.attempt_id]["remote_status"] = "FAILED"
        with self.financial():
            sync_query_feedback(self.memory, as_of=NOW)
        alternate = next(row for row in self.memory.snapshot()["query_outcomes"].values() if "refinement" in row)
        self.assertEqual(alternate["status"], "failed")
        self.assertIsNone(alternate["useful_progress"])
        self.assertTrue(all(a["family_id"] != chosen["family_id"] for a in self.refine()["actions"]))
        self.assertFalse(self.memory.journal.path.exists())

        later = (datetime.fromisoformat(NOW) + timedelta(days=8)).isoformat()
        due = plan_research(self.memory, self.data, as_of=later, slots=1,
                            urgent_family_ids=[chosen["family_id"]])
        self.assertEqual(due["actions"][0]["phrase"], self.action["phrase"])
        self.assertNotIn("refinement", due["actions"][0])

    def test_alias_cannot_reset_pending_refinement_even_when_primary_wording_changes(self):
        self.commit(); self.feedback()
        plan = self.refine(); chosen = plan["actions"][0]
        prepared = self.prepare(chosen, "fixture-alternate")
        bind_prepared_action(self.memory, plan["id"], chosen["action_id"], prepared)
        # The accepted alias remains one explored family even if its canonical
        # representative is retired and its surviving seed has other wording.
        self.data["families"][1]["aliases"] = [self.data["families"][0]["label"]]
        self.data["families"][0]["disposition"] = "retired"
        plan = plan_research(self.memory, self.data, as_of=NOW, slots=12)
        self.assertTrue(all(a["family_id"] not in ("f0-0", "f0-1") for a in plan["actions"]))
        self.assertIn("saved alternative task is unfinished", str(plan["decisions"]))

    def test_another_plan_cannot_bind_a_second_alternate_to_the_same_base(self):
        self.commit(); self.feedback()
        plan = self.refine(); chosen = plan["actions"][0]
        bind_prepared_action(self.memory, plan["id"], chosen["action_id"], self.prepare(chosen, "fixture-alternate"))
        forged = deepcopy(plan)
        forged["id"] = "another-plan"
        forged["actions"][0].update(action_id="another-action", phrase="book shelf holders")
        forged["actions"][0]["request"]["term"] = "book shelf holders"
        self.memory.put("batch_plans", forged["id"], forged)
        with self.assertRaisesRegex(SafetyError, "REFINEMENT_ALREADY_BOUND"):
            bind_prepared_action(self.memory, forged["id"], "another-action", self.prepare(forged["actions"][0], "second-alternate"))

    def test_mismatched_base_capture_cannot_be_bound(self):
        self.commit(); self.feedback()
        plan = self.refine(); chosen = plan["actions"][0]
        plan["actions"][0]["refinement"]["base_capture_hash"] = "0" * 64
        self.memory.put("batch_plans", "tampered-plan", dict(plan, id="tampered-plan"))
        with self.assertRaisesRegex(SafetyError, "REFINEMENT_BASE_REQUIRED"):
            bind_prepared_action(self.memory, "tampered-plan", chosen["action_id"], self.prepare(chosen, "fixture-alternate"))

    def test_generic_rejected_row_is_not_an_empty_diagnostic(self):
        self.commit(values=[{"error": "Other failure"}])
        self.assertNotIn("empty_result", self.feedback())

    def test_rejected_product_identity_is_not_an_empty_diagnostic(self):
        self.commit(values=[{"ad_archive_id": 123}])
        self.assertNotIn("empty_result", self.feedback())

    def test_another_error_code_is_not_an_empty_diagnostic(self):
        self.commit(values=[{"error": "Ads not found", "errorCode": "OTHER", "url": "https://example.org/"}])
        self.assertNotIn("empty_result", self.feedback())

    def test_diagnostic_with_wrong_request_is_rejected_not_used_as_empty(self):
        value = {"error": "Ads not found", "errorCode": "ADS_NOT_FOUND", "url": "https://www.facebook.com/ads/library/?q=other"}
        self.commit(values=[value])
        with self.financial(), self.assertRaisesRegex(SafetyError, "EMPTY_RESULT_PROOF"):
            sync_query_feedback(self.memory)

    def test_body_hash_and_pagination_tampering_cannot_authorise_refinement(self):
        folder = self.commit()
        atomic_bytes(folder / "http/http-fixture.body.json", b"[]")
        with self.financial(), self.assertRaisesRegex(SafetyError, "HASH_MISMATCH"):
            sync_query_feedback(self.memory)

    def test_changed_pagination_is_not_accepted_from_the_diagnostic_text_alone(self):
        folder = self.commit()
        meta = load_json(folder / "http/http-fixture.json")
        meta["headers"]["x-apify-pagination-total"] = "2"
        atomic_json(folder / "http/http-fixture.json", meta)
        with self.financial(), self.assertRaisesRegex(SafetyError, "PAGINATION_INCONSISTENT"):
            sync_query_feedback(self.memory)

    def test_changed_semantic_options_are_not_proven_by_matching_query_text(self):
        self.commit()
        saved = self.memory.snapshot()["query_tasks"][self.task["id"]]
        changed = deepcopy(saved)
        changed["request"]["options"]["media_type"] = "video"
        self.memory.put("query_tasks", self.task["id"], changed, previous=saved)
        with self.financial(), self.assertRaisesRegex(SafetyError, "EMPTY_RESULT_PROOF"):
            sync_query_feedback(self.memory)

    def test_false_empty_marker_cannot_be_imported_without_actual_proof(self):
        self.commit(values=[{"error": "Unknown source error"}])
        self.feedback()
        value = outcome(self.action, days=0, acquisition_id="dataset-fixture-original", resolved_count=0, known_count=0,
            empty_result={"kind": "meta_ads_not_found", "capture_hash": digest([{"error": "Unknown source error"}]),
                          "request_key": semantic_key(self.action["request"]), "provider_items": 1, "ad_rows": 0})
        with self.financial(), self.assertRaisesRegex(SafetyError, "EMPTY_RESULT_PROOF"):
            record_query_outcome(self.memory, value)

    def test_legacy_named_refinement_outcome_keeps_existing_validation_and_recording_behavior(self):
        self.commit(values=[{"error": "Other source error"}])
        original = self.feedback()
        named = dict(original, refinement={"concept_id": "saved-concept", "term": self.action["phrase"],
                                          "reason": "Existing named validation metadata, not empty-discovery refinement."})
        self.assertEqual(validate_outcome(named), named)
        recorded_id = record_query_outcome(self.memory, named)
        self.assertEqual(self.memory.snapshot()["query_outcomes"][recorded_id]["refinement"], named["refinement"])
        plan = self.refine()
        self.assertNotIn("refinement", plan["actions"][0])

    def test_legacy_named_metadata_does_not_match_an_empty_refinement_episode(self):
        self.commit(); base = self.feedback()
        legacy = outcome(self.action, days=0, acquisition_id="legacy-dataset", request=dict(
            self.action["request"], term="other existing phrase"),
            refinement={"concept_id": "saved-concept", "term": "other existing phrase", "reason": "Named old metadata."})
        result = plan_searches(self.data, [base, legacy], as_of=NOW, slots=1,
                              coverage={"limit": 5}, routes={"meta": dict(
                                  available=True, route="apify", build=self.action["request"]["build"], schema_version=1,
                                  date_scope=self.action["request"]["date_scope"], options=self.action["request"]["options"])},
                              urgent_family_ids=[self.action["family_id"]])
        self.assertEqual(result["actions"][0]["phrase"], "fixture bookends")
        self.assertEqual(result["actions"][0]["refinement"]["kind"], "empty_result_alternative")

    def test_forged_request_key_and_boolean_count_markers_are_invalid(self):
        self.commit(); measured = self.feedback()
        for change in ({"request_key": "0" * 64}, {"provider_items": True}, {"ad_rows": False}):
            bad = deepcopy(measured); bad["empty_result"].update(change)
            with self.assertRaisesRegex(SafetyError, "EMPTY_RESULT_INVALID"):
                validate_outcome(bad)

    def test_nonempty_reviewed_and_unreviewed_results_do_not_trigger_alternative(self):
        for reviewed in (False, True):
            previous = outcome(self.action, reviewed=reviewed)
            plan = plan_searches(self.data, [previous], as_of=NOW, slots=1,
                                 coverage={"limit": 5}, urgent_family_ids=[self.action["family_id"]])
            self.assertEqual(plan["actions"][0]["phrase"], self.action["phrase"])
            self.assertNotIn("refinement", plan["actions"][0])

    def test_seed_retains_family_counts_and_only_three_reviewed_alternatives(self):
        data = load_catalogue()
        self.assertEqual(catalogue_summary(data)["distinct_usable_families"], 224)
        for family_id, first, alternative in (
            ("hd-book-supports", "adjustable bookends", "bookends"),
            ("hk-reusable-lint-removers", "fabric lint removal tool", "lint remover"),
            ("to-packing-cubes", "zippered luggage packing cubes", "packing cubes")):
            family = next(row for row in data["families"] if row["id"] == family_id)
            phrases = [p["text"] for p in family["phrases"] if p["source"] == "meta" and p["purpose"] == "discovery"]
            self.assertEqual(phrases, [first, alternative])


if __name__ == "__main__":
    unittest.main()
