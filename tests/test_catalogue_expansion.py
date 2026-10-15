"""Catalogue growth uses saved inputs, not new paid source collection."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.catalogue import validate_catalogue
from product_radar.catalogue_expansion import effective_catalogue, run_expansion
from product_radar.brand_reviews import apply_brand_review, prepare_brand_review
from product_radar.fit_policy import ACTIVE_CRITERION_IDS, FIT_POLICY_ID, evaluate_fit
from product_radar.models import SafetyError
from product_radar.research_ai import Jobs, SUPPLIER_PROMPT_VERSION, prompt_version
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal

NOW = "2026-10-06T10:00:00+00:00"
CATEGORY = "Home and Kitchen"


def fixture():
    return {"schema_version": 1, "revision": "test", "built_at": NOW,
            "sources": [{"id": "page", "url": "https://example.org/storage", "title": "Synthetic page",
                         "checked_at": NOW, "excerpts": ["Test storage only."]}],
            "branches": [{"id": "storage", "parent_id": None, "category": CATEGORY, "name": "Kitchen storage"}],
            "families": [{"id": "caddy", "parent_id": "storage", "category": CATEGORY, "label": "Sink caddy",
                          "definition": "Holds dishwashing tools beside the sink.", "problem": "Wet clutter around the sink.",
                          "included": ["Sink caddy"], "excluded": ["Pot holder"], "aliases": ["Sink organiser"], "overlaps": [],
                          "examples": [{"label": "Sink caddy", "basis": "observed", "sources": ["page"]}],
                          "basis": "observed", "sources": ["page"], "rationale": "Fixture research direction.",
                          "phrases": [phrase("sink caddy")], "season": {"kind": "unknown", "months": []},
                          "review_note": "Fixture only.", "disposition": "usable", "author": "test", "reviewed_at": NOW,
                          "next_review": "After search."}]}


def phrase(label):
    return {"text": label, "source": "meta", "purpose": "discovery", "language": "en", "country": "IN", "reason": "Specific function search."}


class Runner:
    identity = "synthetic-c-v1"

    def __init__(self, mutate=None):
        self.calls = []
        self.mutate = mutate

    def __call__(self, role, payload, folder):
        self.calls.append((role, deepcopy(payload)))
        results = []
        for item in payload["items"]:
            row = {"id": item["id"], "decision": "new_family", "match_id": None, "category": CATEGORY,
                   "label": item["description"]["label"], "problem": "Makes a particular storage use easier.",
                   "distinction": "A different mechanism or useful research question from supplied caddies.",
                   "parent_id": "storage", "basis": "observed", "evidence_ids": [o["id"] for o in item["evidence"]],
                   "primary_phrase": phrase(item["description"]["label"]), "alternative_phrases": [],
                   "reason": "The supplied description identifies a useful distinct function."}
            if self.mutate:
                self.mutate(row, item)
            results.append(row)
        return {"results": results}, {"accounting": "synthetic"}


class ExpansionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.memory = ResearchMemory(root / "research", Journal(root / "financial"), evidence_type="synthetic")
        self.base = fixture()

    def tearDown(self):
        self.folder.cleanup()

    def concept(self, label="Magnetic pan lid rack", *, concept_id="concept1", evidence=True, url="https://example.org/item"):
        description = {"label": label, "category": CATEGORY, "function": "Stores pan lids upright.",
                       "mechanism": "Magnetic wall rail", "audience": "Households", "features": [],
                       "basis": "observed", "brand": "unclear", "reason": "Saved source description."}
        self.memory.put("concepts", concept_id, {"description": description, "status": "provisional"})
        if evidence:
            observation_id = "obs-" + concept_id
            self.memory.put("observations", observation_id, {"source": "meta", "semantic_id": "semantic-" + concept_id,
                "observed_at": NOW, "semantic": {"title": label, "body": "A magnetic rail holds pan lids."}, "facts": {"url": url}})
            self.memory.put("associations", "association-" + concept_id, {"concept_id": concept_id,
                "semantic_id": "semantic-" + concept_id, "status": "resolved"})
        return concept_id

    def run_one(self, runner=None, ids=None):
        return run_expansion(self.memory, self.base, runner, concept_ids=ids or ["concept1"], as_of=NOW)

    def test_observed_admission_and_effective_view_leave_base_unchanged(self):
        self.concept()
        original = deepcopy(self.base)
        result = self.run_one(Runner())
        self.assertEqual(result["records"][0]["outcome"], "accepted")
        effective = effective_catalogue(self.base, self.memory.snapshot())
        self.assertEqual(len(effective["families"]), 2)
        self.assertEqual(effective["sources"][-1]["url"], "https://example.org/item")
        self.assertEqual(self.base, original)
        validate_catalogue(effective)

    def test_unchanged_inputs_are_not_sent_again_and_changed_inputs_are(self):
        self.concept()
        runner = Runner()
        first = self.run_one(runner)
        second = self.run_one(runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(first["records"], second["records"])
        old = self.memory.snapshot()["concepts"]["concept1"]
        new = deepcopy(old)
        new["description"]["mechanism"] = "Different wall rail design"
        self.memory.put("concepts", "concept1", new, previous=old)
        # Existing exact labels merge locally and retain the previous admission.
        third = self.run_one(runner)
        self.assertEqual(third["records"][0]["outcome"], "merged")
        self.assertEqual(len(self.memory.snapshot()["catalogue_additions"]), 2)

    def test_aliases_and_retired_labels_merge_without_a_model(self):
        self.base["families"][0]["disposition"] = "retired"
        self.concept("SINK ORGANISER")
        result = self.run_one()
        self.assertEqual(result["records"][0]["family_id"], "caddy")
        effective = effective_catalogue(self.base, self.memory.snapshot())
        self.assertEqual(effective["families"][0]["disposition"], "retired")
        self.assertEqual(len(effective["families"]), 1)

    def test_semantic_same_family_alias_keeps_existing_identity(self):
        self.concept("Dishwashing tool rail")
        runner = Runner(lambda row, item: row.update(decision="same_family", match_id="caddy"))
        result = self.run_one(runner)
        self.assertEqual(result["records"][0]["outcome"], "merged")
        self.assertIn("Dishwashing tool rail", effective_catalogue(self.base, self.memory.snapshot())["families"][0]["aliases"])

    def test_manufacturer_word_is_not_an_automatic_rejection(self):
        self.concept("TinyFactory magnetic pan lid rack")
        self.assertEqual(self.run_one(Runner())["records"][0]["outcome"], "accepted")

    def test_automatic_unassessed_idea_can_expand_before_recommendation_checks(self):
        self.concept("TinyFactory magnetic pan lid rack")
        before = self.memory.snapshot()
        runner = Runner()
        result = run_expansion(self.memory, self.base, runner, as_of=NOW)
        self.assertEqual(result["records"][0]["outcome"], "accepted")
        self.assertEqual([role for role, payload in runner.calls], ["C"])
        after = self.memory.snapshot()
        for table in ("concepts", "observations", "associations", "assessments", "supplier_assessments", "market_reviews"):
            self.assertEqual(after[table], before[table])
        request = next(iter(after["jobs"].values()))["request"]
        self.assertEqual(request["prompt_version"], "research-c-v2")
        self.assertEqual(set(request["payload"]), {"items"})
        self.assertEqual(set(request["payload"]["items"][0]), {"id", "description", "evidence", "candidates", "branches"})

    def test_automatic_below_five_fit_still_supplies_an_exploration_seed(self):
        concept_id = self.concept()
        criteria = [{"id": number, "state": "Meets" if index < 4 else "Does not meet",
                     "basis": "inferred", "references": ["obs-" + concept_id],
                     "reason": "Synthetic supplied-use judgment, not a market claim."}
                    for index, number in enumerate(ACTIVE_CRITERION_IDS)]
        self.assertFalse(evaluate_fit(criteria, FIT_POLICY_ID)["eligible"])
        self.memory.put("assessments", concept_id, {"fit_policy_id": FIT_POLICY_ID,
            "contract_version": "research-e-fit10-v1", "result": {"criteria": criteria}})
        original = deepcopy(self.memory.snapshot()["assessments"])
        result = run_expansion(self.memory, self.base, Runner(), as_of=NOW)
        self.assertEqual(result["records"][0]["concept_id"], concept_id)
        self.assertEqual(result["records"][0]["outcome"], "accepted")
        self.assertEqual(self.memory.snapshot()["assessments"], original)

    def test_inactive_mainstream_conflicted_and_incoherent_seeds_do_not_take_slots(self):
        changes = ({"status": "excluded"}, {"status": "merged"}, {"status": "retired"},
                   {"identity_conflicted": True}, {"description": {"brand": "established"}},
                   {"description": {"category": "Out of scope"}}, {"description": {"function": " "}})
        excluded_ids = []
        for index, mutation in enumerate(changes):
            concept_id = self.concept("Excluded fixture idea " + str(index), concept_id="concept-0" + str(index))
            old = self.memory.snapshot()["concepts"][concept_id]
            changed = deepcopy(old)
            for field, value in mutation.items():
                if field == "description":
                    changed[field].update(value)
                else:
                    changed[field] = value
            self.memory.put("concepts", concept_id, changed, previous=old)
            excluded_ids.append(concept_id)
        self.concept(concept_id="concept-healthy")
        runner = Runner()
        result = run_expansion(self.memory, self.base, runner, limit=1, as_of=NOW)
        self.assertEqual([record["concept_id"] for record in result["records"]], ["concept-healthy"])
        self.assertEqual([item["id"] for item in runner.calls[0][1]["items"]], ["concept-healthy"])
        skipped = run_expansion(self.memory, self.base, runner, concept_ids=excluded_ids, as_of=NOW)
        self.assertEqual(skipped["records"], [])
        self.assertEqual(len(runner.calls), 1)

    def test_shared_brand_overlays_not_original_brand_wording_select_seeds(self):
        first = self.concept(concept_id="concept1")
        second = self.concept("Folding pan lid shelf", concept_id="concept2")
        old = self.memory.snapshot()["concepts"][second]
        changed = deepcopy(old); changed["description"]["brand"] = "established"
        self.memory.put("concepts", second, changed, previous=old)
        for concept_id, verdict in ((first, "established"), (second, "generic_or_small")):
            request = prepare_brand_review(self.memory.snapshot(), "concept", concept_id,
                verdict=verdict, reason="Synthetic source-bound brand review.",
                evidence_ids=["obs-" + concept_id], reviewer="fixture-reviewer", reviewed_at=NOW)
            apply_brand_review(self.memory, request)
        runner = Runner()
        result = run_expansion(self.memory, self.base, runner, as_of=NOW)
        self.assertEqual([record["concept_id"] for record in result["records"]], [second])
        self.assertEqual(runner.calls[0][1]["items"][0]["description"]["brand"], "established",
                         "A reviewed brand overlay must not rewrite the original C input description.")
        replay = run_expansion(self.memory, self.base, runner, concept_ids=[first, second], as_of=NOW)
        self.assertEqual(replay["records"], result["records"])
        self.assertEqual(len(runner.calls), 1)

    def test_changed_brand_origin_does_not_stop_the_next_healthy_seed(self):
        stale = self.concept(concept_id="concept1")
        old = self.memory.snapshot()["concepts"][stale]
        self.memory.put("concepts", stale, dict(old, origin_child_id="missing-child",
            origin_brand_review_id="missing-review", origin_description_hash="0" * 64), previous=old)
        self.concept("Folding pan lid shelf", concept_id="concept2")
        result = run_expansion(self.memory, self.base, Runner(), limit=1, as_of=NOW)
        self.assertEqual([record["concept_id"] for record in result["records"]], ["concept2"])

    def test_explicit_saved_c_replay_retains_order_and_original_requests(self):
        self.concept(concept_id="concept1")
        self.concept("Folding pan lid shelf", concept_id="concept2")
        runner = Runner()
        first = run_expansion(self.memory, self.base, runner, concept_ids=["concept2", "concept1"], as_of=NOW)
        before = self.memory.snapshot()
        self.assertEqual(next(iter(before["jobs"].values()))["request"]["prompt_version"], "research-c-v2")
        # A later malformed/old recommendation answer cannot change a C question.
        self.memory.put("assessments", "concept1", {"result": {"criteria": []}})
        second = run_expansion(self.memory, self.base, None, concept_ids=["concept1", "concept2"], as_of=NOW)
        self.assertEqual([row["concept_id"] for row in second["records"]], ["concept1", "concept2"])
        self.assertEqual({row["id"]: row for row in first["records"]}, {row["id"]: row for row in second["records"]})
        self.assertEqual(self.memory.snapshot()["jobs"], before["jobs"])
        self.assertEqual(self.memory.snapshot()["catalogue_additions"], before["catalogue_additions"])
        self.assertEqual(len(runner.calls), 1)

    def test_plausible_parent_related_idea_needs_no_proof_search(self):
        self.concept(evidence=False)
        result = self.run_one(Runner(lambda row, item: row.update(basis="plausible_extension")))
        self.assertEqual(result["records"][0]["outcome"], "accepted")
        self.assertEqual(result["records"][0]["family"]["sources"], [])

    def test_invalid_references_are_rejected_but_siblings_continue(self):
        self.concept(concept_id="concept1")
        self.concept("Foldaway pot lid shelf", concept_id="concept2")
        def mutate(row, item):
            if item["id"] == "concept1":
                row["evidence_ids"] = ["invented-page"]
        result = self.run_one(Runner(mutate), ["concept1", "concept2"])
        self.assertEqual([row["outcome"] for row in result["records"]], ["rejected", "accepted"])

    def test_unknown_parent_and_match_and_url_phrase_are_rejected(self):
        for field, value in (("parent_id", "unknown"), ("match_id", "invented"), ("primary_phrase", phrase("https://evil.example"))):
            with self.subTest(field=field):
                runner = Runner(lambda row, item, f=field, v=value: row.update(**{f: v}))
                self.concept(concept_id="concept-" + field)
                result = self.run_one(runner, ["concept-" + field])
                self.assertEqual(result["records"][0]["outcome"], "rejected")

    def test_observed_without_evidence_is_rejected_and_private_url_deferred(self):
        self.concept(evidence=False)
        self.assertEqual(self.run_one(Runner())["records"][0]["outcome"], "rejected")
        self.concept(concept_id="concept2", url="https://example.org?api_key=secret")
        self.assertEqual(self.run_one(Runner(), ["concept2"])["records"][0]["outcome"], "deferred")

    def test_meaningfully_distinct_sibling_is_not_blocked_by_shared_words(self):
        self.concept("Sink caddy magnetic wall rail")
        self.assertEqual(self.run_one(Runner())["records"][0]["outcome"], "accepted")

    def test_jobs_c_bounds_and_old_role_versions_are_preserved(self):
        self.assertEqual(prompt_version("A"), "research-abe-v1")
        self.assertEqual(prompt_version("B"), "research-b-functional-v2")
        self.assertEqual(prompt_version("E"), "research-abe-v1")
        self.assertEqual(prompt_version("F"), SUPPLIER_PROMPT_VERSION)
        self.assertEqual(prompt_version("C"), "research-c-v2")
        with self.assertRaisesRegex(SafetyError, "INPUT_INVALID"):
            Jobs(self.memory, Runner()).run("C", {"items": [{"id": "c" + str(n)} for n in range(6)]})

    def test_nearby_candidates_are_bounded_and_include_retired(self):
        for index in range(10):
            family = deepcopy(self.base["families"][0])
            family.update(id="sibling-" + str(index), label="Different shelf " + str(index), aliases=[])
            self.base["families"].append(family)
        self.base["families"][0]["disposition"] = "retired"
        self.concept("Sink caddy wall variant")
        runner = Runner()
        self.run_one(runner)
        supplied = runner.calls[0][1]["items"][0]["candidates"]
        self.assertEqual(len(supplied), 8)
        self.assertIn("caddy", [row["id"] for row in supplied])

    def test_changed_description_is_bound_to_a_new_model_job(self):
        self.concept()
        runner = Runner()
        self.run_one(runner)
        previous = self.memory.snapshot()["concepts"]["concept1"]
        changed = deepcopy(previous)
        changed["description"].update(label="Expandable pan lid drawer divider", mechanism="Telescopic drawer divider")
        self.memory.put("concepts", "concept1", changed, previous=previous)
        result = self.run_one(runner)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(result["records"][0]["input"]["description"], changed["description"])
        self.assertEqual(len(self.memory.snapshot()["catalogue_additions"]), 2)

    def test_missing_runner_is_saved_as_resumable_and_can_later_finish(self):
        self.concept()
        pending = self.run_one()
        self.assertEqual(pending["pending"], ["concept1"])
        self.assertEqual(pending["records"][0]["outcome"], "deferred")
        self.assertTrue(pending["records"][0]["resumable"])
        finished = self.run_one(Runner())
        self.assertEqual(finished["records"][0]["outcome"], "accepted")
        self.assertEqual(len(self.memory.snapshot()["catalogue_additions"]), 2)

    def test_automatic_later_batches_are_not_starved_by_completed_first_five(self):
        for index in range(6):
            concept_id = "concept-" + str(index)
            self.concept("Distinct storage mechanism " + str(index), concept_id=concept_id)
        runner = Runner()
        first = run_expansion(self.memory, self.base, runner, as_of=NOW)
        second = run_expansion(self.memory, self.base, runner, as_of=NOW)
        self.assertEqual(len(first["records"]), 5)
        self.assertEqual(len(second["records"]), 1)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(second["records"][0]["concept_id"], "concept-5")

    def test_cross_category_and_extra_executable_fields_are_rejected(self):
        self.concept()
        for mutation in (lambda row, item: row.update(category="Baby Products"),
                         lambda row, item: row.update(tool_call={"url": "https://example.org"})):
            # Independent memory avoids an intentionally cached invalid reply.
            with tempfile.TemporaryDirectory() as folder:
                memory = ResearchMemory(Path(folder) / "research", Journal(Path(folder) / "finance"), evidence_type="synthetic")
                for table in ("concepts", "observations", "associations"):
                    for record_id, value in self.memory.snapshot()[table].items():
                        memory.put(table, record_id, value)
                result = run_expansion(memory, self.base, Runner(mutation), concept_ids=["concept1"], as_of=NOW)
                self.assertEqual(result["records"][0]["outcome"], "rejected")

    def test_model_unclear_is_preserved_without_blocking_another_idea(self):
        self.concept()
        self.concept("Folding cookware divider", concept_id="concept2")
        def mutation(row, item):
            if item["id"] == "concept1":
                row.update(decision="unclear", reason="Could overlap with a supplied family; clearer mechanism would help.")
        result = self.run_one(Runner(mutation), ["concept1", "concept2"])
        self.assertEqual([row["outcome"] for row in result["records"]], ["deferred", "accepted"])


if __name__ == "__main__":
    unittest.main()
