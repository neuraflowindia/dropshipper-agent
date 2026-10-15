"""Seed C admission of staged product-map material. Fixtures only; no provider/model calls."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_catalogue_expansion import NOW, fixture, phrase
from product_radar.catalogue_expansion import effective_catalogue
from product_radar import catalogue_seed
from product_radar.catalogue_seed import (MODE, PROMPT_VERSION, build_catalogue_branch, _cached_seed_result,
    _load_material, _seed_item)
from product_radar.map_materials import _row, stage_material
from product_radar.models import SafetyError, digest
from product_radar.research_ai import Jobs, c_contract, prompt_version, validate_results
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.work_turns import bind_work_turn, open_work_turn


def public_receipt(text="Magnetic pan lid rack. Collapsible drying basket."):
    """The saved source text the staged examples are quoted from."""
    return text


class SeedRunner:
    identity = "synthetic-seed-c-v1"
    def __init__(self, change=None): self.calls, self.change = [], change
    def __call__(self, role, payload, folder):
        self.calls.append(deepcopy(payload))
        rows = []
        for item in payload["items"]:
            row = {"id": item["id"], "decision": "new_family", "match_id": None, "category": item["category"],
                "parent_id": item["branch"]["id"], "label": item["material"]["label"], "problem": "Stores a specific tool tidily.",
                "distinction": "A supplied distinct function and attachment mechanism, not a colour or seller change.",
                "basis": "observed", "evidence_ids": [item["evidence"][0]["id"]], "overlap_ids": [],
                "phrases": [phrase(item["material"]["label"])], "action_purpose": "discovery", "reason": "Useful supplied function."}
            if self.change: self.change(row, item)
            rows.append(row)
        return {"results": rows}, {"accounting": "synthetic"}


class CatalogueSeedTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.memory = ResearchMemory(Path(directory.name) / "research", Journal(Path(directory.name) / "finance"), evidence_type="synthetic")
        self.base = fixture()

    def stage(self, examples=None, receipt=None):
        """Stage examples as product-map material (``map_materials.stage_material``):
        each keeps the source line that quotes it."""
        text, branch = receipt or public_receipt(), self.base["branches"][0]
        materials = []
        for example in examples or ["Magnetic pan lid rack"]:
            line = next((row for row in text.splitlines() if example.casefold() in row.casefold()), text)
            materials.append(stage_material(self.memory, branch, _row(
                "platform-item", "fixture:" + example, example, "https://example.org/kitchen-tools",
                "Synthetic kitchen catalogue", NOW, line, "https://example.org/kitchen-tools")))
        return {"materials": materials}

    def busy_commit(self, *, when=lambda changes: True):
        """A research-record write refused for the call's admissions."""
        original = self.memory.commit
        def commit(event_id, changes, **options):
            if any(row["table"] == "catalogue_additions" for row in changes) and when(changes):
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
            return original(event_id, changes, **options)
        return patch.object(self.memory, "commit", new=commit)

    def build(self, runner=None, **kwargs):
        return build_catalogue_branch(self.memory, self.base, runner, branch_id="storage", as_of=NOW, **kwargs)

    def test_observed_admission_and_two_examples_have_conflict_free_source_references(self):
        self.stage(["Magnetic pan lid rack", "Collapsible drying basket"])
        runner = SeedRunner()
        result = self.build(runner)
        self.assertEqual([row["outcome"] for row in result["records"]], ["accepted", "accepted"])
        effective = effective_catalogue(self.base, self.memory.snapshot())
        self.assertEqual(len(effective["families"]), 3)
        self.assertEqual(len({source["id"] for source in effective["sources"]}), 3)
        self.assertEqual(self.memory.snapshot()["concepts"], {})
        self.assertEqual(runner.calls[0]["mode"], MODE)

    def test_source_free_plausible_direction_is_not_described_as_observed_example(self):
        self.stage()
        result = self.build(SeedRunner(lambda row, item: row.update(label="Magnetic kitchen tool board", basis="plausible_extension", evidence_ids=[])))
        family = result["records"][0]["family"]
        self.assertEqual(family["basis"], "plausible_extension")
        self.assertEqual(family["examples"], [{"label": "Magnetic kitchen tool board", "basis": "plausible_extension", "sources": []}])

    def test_no_useful_addition_is_complete_and_cached_without_retrying(self):
        self.stage()
        def none(row, item):
            row.update(decision="no_useful_addition", label="", problem="", distinction="", basis="not_applicable",
                evidence_ids=[], overlap_ids=[], phrases=[], action_purpose="none", reason="The supplied need is already covered; no useful alternative.")
        runner = SeedRunner(none)
        first = self.build(runner); second = self.build(runner)
        self.assertEqual(first["records"][0]["outcome"], "no_useful_addition")
        self.assertEqual(second["reused"], first["records"])
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(second["pending"], [])

    def test_unclear_is_a_saved_decision_not_an_infinite_new_idea_loop(self):
        self.stage()
        runner = SeedRunner(lambda row, item: row.update(decision="unclear", phrases=[], action_purpose="none"))
        first = self.build(runner); self.build(runner)
        self.assertEqual(first["records"][0]["outcome"], "deferred")
        self.assertEqual(len(runner.calls), 1)

    def test_a_new_heading_needs_a_meta_discovery_phrase_and_other_sources_are_extras(self):
        self.stage(["Magnetic pan lid rack", "Collapsible drying basket"])
        def phrases(row, item):
            if item["material"]["label"] == "Magnetic pan lid rack":
                row["phrases"][0].update(source="supplier", purpose="discovery")  # no Meta phrase: never searched
            else:
                row["phrases"].append(dict(row["phrases"][0], source="dropship", purpose="validation"))
        result = self.build(SeedRunner(phrases))
        outcomes = {row["input"]["material"]["label"]: row for row in result["records"]}
        self.assertEqual(outcomes["Magnetic pan lid rack"]["outcome"], "rejected")
        self.assertIn("CATALOGUE_SEED_DISCOVERY_PHRASE_REQUIRED", outcomes["Magnetic pan lid rack"]["reason"])
        family = effective_catalogue(self.base, self.memory.snapshot())["families"][-1]
        self.assertEqual([phrase["source"] for phrase in family["phrases"]], ["meta", "dropship"])
        self.assertEqual(result["provider_calls"], 0)

    def test_same_retired_family_revisit_preserves_identity_and_history(self):
        self.base["families"][0]["disposition"] = "retired"
        self.stage()
        runner = SeedRunner(lambda row, item: row.update(decision="same_family", match_id="caddy", action_purpose="revisit"))
        result = self.build(runner)
        effective = effective_catalogue(self.base, self.memory.snapshot())
        self.assertEqual(result["records"][0]["family_id"], "caddy")
        self.assertEqual(effective["families"][0]["disposition"], "retired")
        self.assertEqual(len(effective["families"]), 1)

    def test_exact_existing_example_is_merged_without_any_model_job(self):
        self.stage(["Sink organiser"], public_receipt("Sink organiser"))
        result = self.build()
        self.assertEqual(result["records"][0]["family_id"], "caddy")
        self.assertEqual(result["job_ids"], [])

    def test_bad_reference_preserves_healthy_sibling_and_caches_rejection(self):
        self.stage(["Magnetic pan lid rack", "Collapsible drying basket"])
        def mutate(row, item):
            if item["material"]["label"] == "Magnetic pan lid rack": row["evidence_ids"] = ["invented-reference"]
        runner = SeedRunner(mutate)
        first = self.build(runner); self.build(runner)
        self.assertEqual(sorted(row["outcome"] for row in first["records"]), ["accepted", "rejected"])
        self.assertEqual(len(runner.calls), 1)

    def test_saved_original_result_survives_local_commit_failure_and_changed_nearby_catalogue(self):
        self.stage(["Magnetic pan lid rack", "Collapsible drying basket"])
        runner = SeedRunner()
        with self.busy_commit():
            first = self.build(runner)
        # The call's admissions are saved together: a refused write leaves them all pending.
        self.assertEqual((len(first["pending"]), first["records"]), (2, []))
        second = self.build(runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(second["pending"], [])
        self.assertEqual(len(effective_catalogue(self.base, self.memory.snapshot())["families"]), 3)

    def _unadmitted_original(self, labels, runner=None):
        staged = self.stage(labels, public_receipt(". ".join(labels)))
        runner = runner or SeedRunner()
        ids = [row["id"] for row in staged["materials"]]
        with self.busy_commit():
            result = self.build(runner, material_ids=ids)
        self.assertEqual(len(result["pending"]), len(labels))
        return runner, ids, result["job_ids"][0]

    def test_ten_saved_original_rows_replay_one_batch_and_keep_ten_individual_admissions(self):
        runner, ids, job_id = self._unadmitted_original(
            ["Magnetic pan lid rack " + str(number) for number in range(10)])
        original_run = Jobs.run
        original_commit = self.memory.commit
        with patch.object(Jobs, "run", autospec=True, side_effect=original_run) as jobs, \
                patch.object(self.memory, "commit", wraps=original_commit) as commits:
            result = self.build(runner, material_ids=ids)
        self.assertEqual(jobs.call_count, 1)
        self.assertEqual(len(result["records"]), 10)
        self.assertEqual(result["pending"], [])
        self.assertEqual(result["job_ids"], [job_id])
        # Ten individual admissions, saved in one write of the research records.
        self.assertEqual([len(call.args[1]) for call in commits.call_args_list], [10])
        self.assertEqual(len(runner.calls), 1)

    def test_replay_reuse_does_not_hide_material_changes_between_siblings(self):
        runner, ids, job_id = self._unadmitted_original(["Magnetic pan lid rack", "Collapsible drying basket"])
        original_admit = catalogue_seed._admit
        def change_next_material(catalogue, item, proposal, at):
            result = original_admit(catalogue, item, proposal, at)
            if item["id"] == ids[0]:
                path = self.memory.root / "catalogue_materials/items" / ids[1] / "material.json"
                saved = load_json(path); saved["receipt_hash"] = "0" * 64
                atomic_json(path, saved)
            return result
        original_run = Jobs.run
        with patch.object(Jobs, "run", autospec=True, side_effect=original_run) as jobs, \
                patch.object(catalogue_seed, "_admit", new=change_next_material):
            result = self.build(runner, material_ids=ids)
        self.assertEqual(jobs.call_count, 1)
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["pending"], [ids[1]])
        self.assertEqual(len(runner.calls), 1)

    def test_replay_reuse_does_not_hide_changed_or_foreign_saved_answer_between_siblings(self):
        runner, ids, job_id = self._unadmitted_original(["Magnetic pan lid rack", "Collapsible drying basket"])
        path = self.memory.root / "ai_jobs" / job_id / "result.json"
        original_admit = catalogue_seed._admit
        def change_answer(catalogue, item, proposal, at):
            result = original_admit(catalogue, item, proposal, at)
            if item["id"] == ids[0]:
                saved = load_json(path); saved["request_hash"] = "0" * 64
                atomic_json(path, saved)
            return result
        original_run = Jobs.run
        with patch.object(Jobs, "run", autospec=True, side_effect=original_run) as jobs, \
                patch.object(catalogue_seed, "_admit", new=change_answer):
            result = self.build(runner, material_ids=ids)
        self.assertEqual(jobs.call_count, 2)
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["pending"], [ids[1]])
        self.assertEqual(result["failures"][0]["reason"], "AI_JOB_RESULT_CONFLICT")
        self.assertEqual(len(runner.calls), 1)

    def test_original_replay_cache_is_isolated_between_jobs(self):
        runner, first_ids, first_job = self._unadmitted_original(
            ["Magnetic pan lid rack", "Collapsible drying basket"])
        runner, second_ids, second_job = self._unadmitted_original(
            ["Folding saucepan holder", "Hanging kitchen basket"], runner)
        original_run = Jobs.run
        with patch.object(Jobs, "run", autospec=True, side_effect=original_run) as jobs:
            result = self.build(runner, material_ids=first_ids + second_ids)
        self.assertEqual(jobs.call_count, 2)
        self.assertEqual(len(result["records"]), 4)
        self.assertEqual(set(result["job_ids"]), {first_job, second_job})
        self.assertEqual(result["pending"], [])
        self.assertEqual(len(runner.calls), 2)

    def test_corrupt_original_does_not_contaminate_a_healthy_original_batch(self):
        runner, bad_ids, bad_job = self._unadmitted_original(
            ["Magnetic pan lid rack", "Collapsible drying basket"])
        runner, healthy_ids, healthy_job = self._unadmitted_original(
            ["Folding saucepan holder", "Hanging kitchen basket"], runner)
        path = self.memory.root / "ai_jobs" / bad_job / "result.json"
        saved = load_json(path); saved["request_hash"] = "0" * 64; atomic_json(path, saved)
        original_run = Jobs.run
        with patch.object(Jobs, "run", autospec=True, side_effect=original_run) as jobs:
            result = self.build(runner, material_ids=bad_ids + healthy_ids)
        self.assertEqual(jobs.call_count, 3)  # Failed originals are not memoised.
        self.assertEqual({row["material_id"] for row in result["records"]}, set(healthy_ids))
        self.assertEqual(set(result["pending"]), set(bad_ids))
        self.assertEqual(result["job_ids"], [healthy_job])
        self.assertEqual({row["reason"] for row in result["failures"]}, {"AI_JOB_RESULT_CONFLICT"})
        self.assertEqual(len(runner.calls), 2)

    def test_uncertain_replay_is_not_cached_for_the_next_healthy_sibling(self):
        runner, ids, job_id = self._unadmitted_original(["Magnetic pan lid rack", "Collapsible drying basket"])
        original_run = Jobs.run
        calls = []
        def initially_waiting(jobs, *args, **options):
            calls.append(1)
            if len(calls) == 1:
                raise SafetyError("CATALOGUE_SEED_SAVED_RESULT_NOT_READY")
            return original_run(jobs, *args, **options)
        with patch.object(Jobs, "run", new=initially_waiting):
            result = self.build(runner, material_ids=ids)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["pending"], [ids[0]])
        self.assertEqual([row["material_id"] for row in result["records"]], [ids[1]])
        self.assertEqual(self.build(runner, material_ids=ids)["pending"], [])
        self.assertEqual(len(runner.calls), 1)

    def test_successful_replay_reuse_does_not_survive_a_build_invocation(self):
        runner, ids, job_id = self._unadmitted_original(["Magnetic pan lid rack", "Collapsible drying basket"])
        original_run = Jobs.run
        with patch.object(Jobs, "run", autospec=True, side_effect=original_run) as jobs, self.busy_commit():
            for _ in range(2):
                self.assertEqual(set(self.build(runner, material_ids=ids)["pending"]), set(ids))
        self.assertEqual(jobs.call_count, 2)
        self.assertEqual(len(runner.calls), 1)

    def test_original_seed_selector_uses_supplied_history_for_advisory_turn_check(self):
        self.stage()
        runner = SeedRunner()
        first = self.build(runner)
        job_id = first["job_ids"][0]
        runner.closed_retry_policy = {"as_of": lambda: NOW, "retry_seconds": 300,
                                      "retry_not_before": lambda: None}
        turn = open_work_turn(self.memory, "seed-original-selection", at=NOW)
        with bind_work_turn(self.memory, turn["id"]):
            state = self.memory.snapshot()
            before = deepcopy(state)
            request = state["jobs"][job_id]["request"]
            saved = load_json(self.memory.root / "ai_jobs" / job_id / "result.json")
            checked = validate_results("C", request["payload"], saved["response"], declared_version=PROMPT_VERSION)
            # Isolate selection from the Jobs boundary, which keeps its own
            # fresh original-file, admission and execution checks.
            with patch.object(self.memory, "snapshot", side_effect=AssertionError("Unexpected advisory history read")), \
                    patch.object(Jobs, "run", return_value=(job_id, checked)) as jobs:
                cached = _cached_seed_result(self.memory, state, request["payload"]["items"][0], runner=runner)
        self.assertEqual(cached, (job_id, request["payload"]["items"][0], checked))
        jobs.assert_called_once_with("C", request["payload"], declared_version=PROMPT_VERSION)
        self.assertEqual(state, before)
        self.assertEqual(len(runner.calls), 1)

    def test_original_seed_result_without_optional_context_is_replayed_without_another_model(self):
        self.stage(["Magnetic pan lid rack"])
        runner = SeedRunner()
        def legacy_material(material):
            return {name: material[name] for name in ("id", "label", "category", "branch_id", "basis")}
        with patch("product_radar.catalogue_seed._input_material", side_effect=legacy_material), self.busy_commit():
            first = self.build(runner)
        self.assertEqual(len(first["pending"]), 1)
        self.assertNotIn("context_excerpt", runner.calls[0]["items"][0]["material"])
        result = self.build(runner)
        self.assertEqual(result["pending"], [])
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(len(runner.calls), 1)
        self.assertNotIn("context_excerpt", result["records"][0]["input"]["material"])

    def test_material_tampering_defers_only_that_example_before_model_use(self):
        staged = self.stage(["Magnetic pan lid rack", "Collapsible drying basket"])
        material = staged["materials"][0]
        path = self.memory.root / "catalogue_materials/items" / material["id"] / "material.json"
        atomic_json(path, dict(material, source=dict(material["source"], url="https://invented.example.org/")))
        runner = SeedRunner()
        result = self.build(runner)
        self.assertEqual(len(result["failures"]), 1)
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(len(runner.calls[0]["items"]), 1)

    def test_seed_contract_does_not_reinterpret_legacy_c_or_product_query_jobs(self):
        self.assertEqual(prompt_version("C", {"items": []}), "research-c-v2")
        self.assertEqual(prompt_version("C", {"mode": "product_query_profile", "items": []}, "research-c-query-profile-v1"), "research-c-query-profile-v1")
        payload = {"mode": MODE, "seed_version": "catalogue-branch-seed-v1", "items": []}
        self.assertEqual(c_contract(payload)["version"], PROMPT_VERSION)
        with self.assertRaisesRegex(SafetyError, "AI_C_CONTRACT_INVALID"):
            c_contract(payload, "research-c-v1")

    def test_short_quote_keeps_the_actual_product_line_as_context_not_as_a_search_phrase(self):
        source = public_receipt("A supplier advertisement.\nBottle cleanser with rotating nipple cleaning head.\nAnother unrelated row.")
        staged = self.stage(["rotating nipple cleaning head"], source)
        material = _load_material(self.memory, staged["materials"][0]["id"])
        item = _seed_item(self.base, self.base["branches"][0], material)
        self.assertEqual(item["material"]["label"], "rotating nipple cleaning head")
        self.assertEqual(item["material"]["context_excerpt"], "Bottle cleanser with rotating nipple cleaning head.")
        self.assertFalse(item["material"]["context_truncated"])

    def test_short_adjective_retrieves_existing_family_from_actual_source_context(self):
        known = deepcopy(self.base["families"][0])
        known.update(id="pillow", label="Sleeping pillow", aliases=[],
            definition="Supports the head comfortably while sleeping.", problem="Uncomfortable sleep support.")
        self.base["families"].append(known)
        staged = self.stage(["Adjustable Height"], public_receipt("Standard Sleeping Pillow with Adjustable Height and removable filling."))
        item = _seed_item(self.base, self.base["branches"][0], _load_material(self.memory, staged["materials"][0]["id"]))
        self.assertEqual(item["material"]["label"], "Adjustable Height")
        self.assertEqual(item["candidates"][0]["id"], "pillow")

    def test_material_from_a_retired_page_kind_is_not_built(self):
        staged = self.stage()
        material = staged["materials"][0]
        page = load_json(material["page_reference"])
        receipt = dict(page["receipt"], origin={"kind": "verified_marketplace"})
        from product_radar.models import digest
        atomic_json(material["page_reference"], dict(page, receipt=receipt, hash=digest(receipt)))
        atomic_json(self.memory.root / "catalogue_materials/items" / material["id"] / "material.json",
                    dict(material, receipt_hash=digest(receipt)))
        with self.assertRaisesRegex(SafetyError, "CATALOGUE_SEED_MATERIAL_CHANGED"):
            _load_material(self.memory, material["id"])
        self.assertEqual(self.build(SeedRunner())["failures"][0]["reason"], "CATALOGUE_SEED_MATERIAL_CHANGED")

if __name__ == "__main__": unittest.main()
