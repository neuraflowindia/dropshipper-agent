"""Upfront public-material admission. Fixtures only; no provider/model calls."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_catalogue_expansion import CATEGORY, NOW, fixture, phrase
from product_radar.catalogue_expansion import effective_catalogue
from product_radar.catalogue_seed import (MODE, PROMPT_VERSION, build_catalogue_branch, stage_page_materials,
    stage_marketplace_materials, prepare_catalogue_branch_inputs, _cached_seed_result)
from product_radar.models import SafetyError, digest
from product_radar.research_ai import Jobs, c_contract, prompt_version, validate_results
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.work_turns import bind_work_turn, open_work_turn


def public_receipt(text="Magnetic pan lid rack. Collapsible drying basket."):
    return {"schema_version": 2, "id": "public-material", "requested_url": "https://example.org/kitchen-tools",
        "final_url": "https://example.org/kitchen-tools", "checked_at": NOW, "title": "Synthetic kitchen catalogue",
        "text": text, "state": "usable", "reason": "Actual fixture page", "http_status": 200, "body_complete": True,
        "execution_stage": "response_received", "attempt_started_at": NOW, "execution_ref": "public-fetch-" + "a" * 32}


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
        return stage_page_materials(self.memory, self.base, branch_id="storage", receipt=receipt or public_receipt(),
            examples=examples or ["Magnetic pan lid rack"], receipt_reference="saved-fixture-page")

    def build(self, runner=None, **kwargs):
        return build_catalogue_branch(self.memory, self.base, runner, branch_id="storage", as_of=NOW, **kwargs)

    def test_staging_is_public_fact_preparation_not_product_or_assessment_creation(self):
        before = deepcopy(self.memory.snapshot())
        staged = self.stage()
        self.assertEqual((staged["provider_calls"], staged["model_calls"]), (0, 0))
        self.assertEqual(self.memory.snapshot(), before)
        self.assertTrue(Path(staged["reference"]).exists())

    def test_invented_excerpt_and_unusable_or_unproved_captured_page_are_rejected(self):
        with self.assertRaisesRegex(SafetyError, "EXCERPT_NOT_SUPPLIED"):
            self.stage(["Invented wireless robot tool"])
        for change in ({"http_status": 429}, {"body_complete": False}, {"state": "unavailable"}):
            with self.assertRaisesRegex(SafetyError, "PUBLIC_MATERIAL_INVALID"):
                self.stage(receipt=dict(public_receipt(), **change))
        captured = ResearchMemory(self.memory.root / "captured", self.memory.journal, evidence_type="captured")
        with self.assertRaisesRegex(SafetyError, "PUBLIC_MATERIAL_INVALID"):
            stage_page_materials(captured, self.base, branch_id="storage", examples=["Magnetic pan lid rack"],
                receipt=dict(public_receipt(), execution_stage="not_dispatched", attempt_started_at=None, execution_ref=None))

    def test_identical_later_page_keeps_original_material_date_and_zero_extra_work(self):
        first = self.stage()
        second = self.stage(receipt=dict(public_receipt(), checked_at="2026-10-07T10:00:00+00:00", attempt_started_at="2026-10-07T10:00:00+00:00"))
        self.assertEqual(first, second)
        self.assertEqual(second["materials"][0]["source"]["checked_at"], NOW)

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

    def test_suppliers_can_be_research_direction_without_fabricated_meta_phrase(self):
        self.stage()
        def supplier(row, item): row["phrases"][0].update(source="supplier", purpose="discovery")
        result = self.build(SeedRunner(supplier))
        family = effective_catalogue(self.base, self.memory.snapshot())["families"][-1]
        self.assertEqual(family["phrases"][0]["source"], "supplier")
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
        original = self.memory.put
        def busy(table, record_id, value, **kwargs):
            if table == "catalogue_additions" and value["input"]["material"]["label"] == "Magnetic pan lid rack":
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
            return original(table, record_id, value, **kwargs)
        with patch.object(self.memory, "put", side_effect=busy):
            first = self.build(runner)
        self.assertEqual(len(first["pending"]), 1)
        second = self.build(runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(second["pending"], [])
        self.assertEqual(len(effective_catalogue(self.base, self.memory.snapshot())["families"]), 3)

    def _unadmitted_original(self, labels, runner=None):
        staged = self.stage(labels, public_receipt(". ".join(labels)))
        runner = runner or SeedRunner()
        original_put = self.memory.put
        def busy(table, record_id, value, **options):
            if table == "catalogue_additions":
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
            return original_put(table, record_id, value, **options)
        ids = [row["id"] for row in staged["materials"]]
        with patch.object(self.memory, "put", new=busy):
            result = self.build(runner, material_ids=ids)
        self.assertEqual(len(result["pending"]), len(labels))
        return runner, ids, result["job_ids"][0]

    def test_ten_saved_original_rows_replay_one_batch_and_keep_ten_individual_admissions(self):
        runner, ids, job_id = self._unadmitted_original(
            ["Magnetic pan lid rack " + str(number) for number in range(10)])
        original_run = Jobs.run
        original_put = self.memory.put
        with patch.object(Jobs, "run", autospec=True, side_effect=original_run) as jobs, \
                patch.object(self.memory, "put", wraps=original_put) as puts:
            result = self.build(runner, material_ids=ids)
        self.assertEqual(jobs.call_count, 1)
        self.assertEqual(len(result["records"]), 10)
        self.assertEqual(result["pending"], [])
        self.assertEqual(result["job_ids"], [job_id])
        self.assertEqual(sum(call.args[0] == "catalogue_additions" for call in puts.call_args_list), 10)
        self.assertEqual(len(runner.calls), 1)

    def test_replay_reuse_does_not_hide_material_changes_between_siblings(self):
        runner, ids, job_id = self._unadmitted_original(["Magnetic pan lid rack", "Collapsible drying basket"])
        original_put = self.memory.put
        def change_next_material(table, record_id, value, **options):
            result = original_put(table, record_id, value, **options)
            if table == "catalogue_additions" and value["material_id"] == ids[0]:
                path = self.memory.root / "catalogue_materials/items" / ids[1] / "material.json"
                saved = load_json(path); saved["receipt_hash"] = "0" * 64
                atomic_json(path, saved)
            return result
        original_run = Jobs.run
        with patch.object(Jobs, "run", autospec=True, side_effect=original_run) as jobs, \
                patch.object(self.memory, "put", new=change_next_material):
            result = self.build(runner, material_ids=ids)
        self.assertEqual(jobs.call_count, 1)
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["pending"], [ids[1]])
        self.assertEqual(len(runner.calls), 1)

    def test_replay_reuse_does_not_hide_changed_or_foreign_saved_answer_between_siblings(self):
        runner, ids, job_id = self._unadmitted_original(["Magnetic pan lid rack", "Collapsible drying basket"])
        path = self.memory.root / "ai_jobs" / job_id / "result.json"
        original_put = self.memory.put
        def change_answer(table, record_id, value, **options):
            result = original_put(table, record_id, value, **options)
            if table == "catalogue_additions" and value["material_id"] == ids[0]:
                saved = load_json(path); saved["request_hash"] = "0" * 64
                atomic_json(path, saved)
            return result
        original_run = Jobs.run
        with patch.object(Jobs, "run", autospec=True, side_effect=original_run) as jobs, \
                patch.object(self.memory, "put", new=change_answer):
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
        original_put = self.memory.put
        def still_busy(table, record_id, value, **options):
            if table == "catalogue_additions":
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
            return original_put(table, record_id, value, **options)
        with patch.object(Jobs, "run", autospec=True, side_effect=original_run) as jobs, \
                patch.object(self.memory, "put", new=still_busy):
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
        original = self.memory.put
        def busy(table, record_id, value, **kwargs):
            if table == "catalogue_additions":
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
            return original(table, record_id, value, **kwargs)
        def legacy_material(material):
            return {name: material[name] for name in ("id", "label", "category", "branch_id", "basis")}
        with patch("product_radar.catalogue_seed._input_material", side_effect=legacy_material), \
                patch.object(self.memory, "put", side_effect=busy):
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
        self.assertEqual(prompt_version("C", {"items": []}), "research-c-v1")
        self.assertEqual(prompt_version("C", {"mode": "product_query_profile", "items": []}, "research-c-query-profile-v1"), "research-c-query-profile-v1")
        payload = {"mode": MODE, "seed_version": "catalogue-branch-seed-v1", "items": []}
        self.assertEqual(c_contract(payload)["version"], PROMPT_VERSION)
        with self.assertRaisesRegex(SafetyError, "AI_C_CONTRACT_INVALID"):
            c_contract(payload, "research-c-v1")

    def test_preparing_grounded_seed_inputs_during_writer_work_does_not_create_a_job_or_canonical_entry(self):
        staged = self.stage(["Magnetic pan lid rack", "Collapsible drying basket"]); before = self.memory.snapshot()
        result = prepare_catalogue_branch_inputs(self.memory, self.base, branch_id="storage",
            material_ids=[row["id"] for row in staged["materials"]])
        self.assertEqual(result["prompt_version"], PROMPT_VERSION)
        self.assertEqual(len(result["payload"]["items"]), 2)
        self.assertEqual(result["canonical_writes"], 0)
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(before, self.memory.snapshot())

    def test_short_quote_keeps_the_actual_product_line_as_context_not_as_a_search_phrase(self):
        source = public_receipt("A supplier advertisement.\nBottle cleanser with rotating nipple cleaning head.\nAnother unrelated row.")
        self.stage(["rotating nipple cleaning head"], source)
        result = prepare_catalogue_branch_inputs(self.memory, self.base, branch_id="storage")
        item = result["payload"]["items"][0]
        self.assertEqual(item["material"]["label"], "rotating nipple cleaning head")
        self.assertEqual(item["material"]["context_excerpt"], "Bottle cleanser with rotating nipple cleaning head.")
        self.assertFalse(item["material"]["context_truncated"])
        self.assertEqual(result["model_calls"], 0)

    def test_short_adjective_retrieves_existing_family_from_actual_source_context(self):
        known = deepcopy(self.base["families"][0])
        known.update(id="pillow", label="Sleeping pillow", aliases=[],
            definition="Supports the head comfortably while sleeping.", problem="Uncomfortable sleep support.")
        self.base["families"].append(known)
        self.stage(["Adjustable Height"], public_receipt("Standard Sleeping Pillow with Adjustable Height and removable filling."))
        prepared = prepare_catalogue_branch_inputs(self.memory, self.base, branch_id="storage")
        item = prepared["payload"]["items"][0]
        self.assertEqual(item["material"]["label"], "Adjustable Height")
        self.assertEqual(item["candidates"][0]["id"], "pillow")
        self.assertEqual(prepared["model_calls"], 0)

    def test_equivalent_repeated_excerpt_keeps_first_actual_wording_without_duplicate_material(self):
        receipt = public_receipt("Magnetic pan lid rack.")
        first = self.stage(["Magnetic pan lid rack"], receipt)
        second = self.stage(["MAGNETIC  PAN LID RACK"], receipt)
        self.assertEqual(second, first)

    def test_verified_marketplace_page_can_be_staged_without_import_or_another_browser(self):
        from product_radar.marketplace_browser import build_marketplace_request, capture_marketplace
        from test_marketplace_browser import snapshot, transport
        request = build_marketplace_request("amazon-bestsellers", department="kitchen")
        snap = snapshot(request, text="Sink caddy pack of 2", text_truncated=False)
        capture_root = self.memory.root / "public-fixture"
        capture_marketplace(request, "saved-fixture", data_root=capture_root,
            snapshot_reader=lambda _request: transport(snap))
        folder = capture_root / "saved-fixture"; before = self.memory.snapshot()
        staged = stage_marketplace_materials(self.memory, self.base, branch_id="storage", folder=folder)
        self.assertEqual(len(staged["materials"]), 1)
        self.assertEqual(staged["model_calls"], 0)
        prepared = prepare_catalogue_branch_inputs(self.memory, self.base, branch_id="storage",
            material_ids=[staged["materials"][0]["id"]])
        self.assertEqual(len(prepared["payload"]["items"]), 1)
        self.assertEqual(before, self.memory.snapshot())

    def test_changed_original_marketplace_artifact_defers_material_instead_of_model_use(self):
        from product_radar.marketplace_browser import build_marketplace_request, capture_marketplace
        from test_marketplace_browser import snapshot, transport
        request = build_marketplace_request("amazon-bestsellers", department="kitchen")
        snap = snapshot(request, text="Sink caddy pack of 2", text_truncated=False)
        folder = self.memory.root / "public-fixture" / "saved-fixture"
        capture_marketplace(request, "saved-fixture", data_root=folder.parent,
            snapshot_reader=lambda _request: transport(snap))
        staged = stage_marketplace_materials(self.memory, self.base, branch_id="storage", folder=folder)
        changed = load_json(folder / "snapshot.json"); changed["text"] = "Unrelated page"
        atomic_json(folder / "snapshot.json", changed)
        result = prepare_catalogue_branch_inputs(self.memory, self.base, branch_id="storage",
            material_ids=[staged["materials"][0]["id"]])
        self.assertEqual(result["payload"]["items"], [])
        self.assertEqual(len(result["pending"]), 1)
        self.assertEqual(self.memory.snapshot()["jobs"], {})


if __name__ == "__main__": unittest.main()
