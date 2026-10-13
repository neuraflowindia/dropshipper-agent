"""Prepared C admission through the real finite callback; fixtures only."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.catalogue_discovery import (SEED_CYCLE_VERSION, VERSION,
    advance_catalogue_seed_cycle, create_catalogue_discovery_step, prepare_catalogue_seed_cycle,
    prepare_discovery_cycle)
from product_radar.catalogue_expansion import effective_catalogue
from product_radar.catalogue_seed import (PROMPT_VERSION, build_catalogue_branch,
    load_prepared_catalogue_branch, prepare_catalogue_branch_inputs, stage_page_materials)
from product_radar.models import SafetyError, digest
from product_radar.programme import STAGES, advance_programme, load_programme, prepare_programme
from product_radar.research_memory import ResearchMemory, key
from product_radar.storage import Journal, atomic_json, load_json
from test_catalogue_expansion import fixture
from test_catalogue_seed import SeedRunner, public_receipt

NOW = "2026-10-08T10:00:00+00:00"
LATER = "2026-10-08T10:06:00+00:00"


class SeedDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.base = fixture(); self.calls = []
        self.owner = {"programme_id": "original156", "concept_ids": ["original-product"], "source_run_id": "original-source-run"}
        self.memory.put("concepts", "original-product", {"status": "active", "description": {"label": "Existing product"}})

    def prepared(self, labels=("Magnetic pan lid rack",), *, branch="storage", limit=10):
        if branch != "storage" and not any(row["id"] == branch for row in self.base["branches"]):
            self.base["branches"].append(dict(self.base["branches"][0], id=branch, name="A separate kitchen use"))
        staged = stage_page_materials(self.memory, self.base, branch_id=branch,
            receipt=public_receipt(".\n".join(labels)), examples=list(labels), receipt_reference="synthetic-retained-page")
        ids = [row["id"] for row in staged["materials"]]
        prepared = prepare_catalogue_branch_inputs(self.memory, self.base, branch_id=branch, material_ids=ids, limit=limit)
        hashed = digest(prepared["payload"])
        path = self.memory.root / "catalogue_materials/preparations/synthetic-inventory" / (branch + "-" + hashed[:24] + ".json")
        atomic_json(path, prepared)
        return {"branch_id": branch, "material_ids": ids, "prepared_material_ids": prepared["material_ids"],
            "reused_material_ids": prepared["reused_material_ids"], "unprepared_material_ids": sorted(set(ids) - set(prepared["material_ids"])
                - set(prepared["reused_material_ids"]) - {row["material_id"] for row in prepared["pending"]}),
            "pending": prepared["pending"], "payload_hash": hashed, "prepared_reference": str(path), "prompt_version": PROMPT_VERSION}

    def manifest(self, *rows, saved=True):
        manifest = {"schema_version": 1, "inventory_id": "synthetic-inventory", "plan_hash": digest(rows),
            "prepared_at": NOW, "state": "prepared", "branches": list(rows), "inspected": [], "failures": [],
            "prepared_branches": len(rows), "material_examples": sum(len(row["material_ids"]) for row in rows),
            "prepared_examples": sum(len(row["prepared_material_ids"]) for row in rows),
            "canonical_writes": 0, "provider_calls": 0, "model_calls": 0}
        if not saved:
            return manifest
        path = self.memory.root / "catalogue_materials/preparations/manifest.json"; atomic_json(path, manifest)
        return path

    def handler(self, context):
        self.calls.append(deepcopy({k: v for k, v in context.items() if k != "record_dispatch"}))
        aid = context["acquisition_id"]; capture_id = key("capture", ["amazon", aid])
        raw = {"route": context["route"], "acquisition_id": aid, "state": "empty"}
        path = self.memory.root / "public-fixture" / aid / "receipt.json"; atomic_json(path, raw)
        self.memory.put("captures", capture_id, {"source": "amazon", "acquisition_id": aid,
            "observed_at": NOW, "capture_hash": digest(raw), "mapping_status": "empty",
            "reference": {"route": context["route"], "request": context["request"]}},
            previous=self.memory.snapshot()["captures"].get(capture_id))
        context["record_dispatch"]()
        return {"schema_version": 1, "route": context["route"], "acquisition_id": aid,
            "observed_at": NOW, "state": "empty", "dispatch_started": True, "reference": str(path),
            "capture_ids": [capture_id], "observation_ids": [], "target_results": [], "reason": "Synthetic completed empty page."}

    def step(self, manifest, **options):
        return create_catalogue_discovery_step(self.base, include_lists=False, seed_manifest=manifest, **options)

    def call(self, step, runner=None, *, at=NOW):
        return step(self.memory, self.owner, {"at": at, "runner": runner, "handlers": {"amazon-search": self.handler}, "fit_limit": 10})

    def test_constructor_only_prepares_callback_and_final_manifest_structure_is_accepted(self):
        manifest = self.manifest(self.prepared()); before = self.memory.snapshot()
        step = self.step(manifest)
        self.assertEqual(before, self.memory.snapshot()); self.assertEqual(self.calls, [])
        runner = SeedRunner(); result = self.call(step, runner)
        self.assertEqual(result["seed"]["counts"]["actual_judged_examples"], 1)
        self.assertEqual(result["seed"]["counts"]["accepted_examples"], 1)
        self.assertEqual(len(runner.calls), 1)

    def test_new_cycle_chooses_only_actually_admitted_family_after_seed_not_prepared_material(self):
        self.base["families"][0]["disposition"] = "retired"
        row = self.prepared(); manifest = self.manifest(row); original = deepcopy(self.owner)
        payload = load_json(Path(row["prepared_reference"]), 4 * 1024 * 1024)["payload"]
        runner = SeedRunner(); result = self.call(self.step(manifest, action_limit=1), runner)
        self.assertEqual(runner.calls[0], payload)
        cycle = self.memory.snapshot()["batch_runs"][result["cycle_id"]]
        self.assertEqual(cycle["planned_actions"][0]["phrase"], "Magnetic pan lid rack")
        self.assertEqual(cycle["baseline_concepts"], ["original-product"])
        self.assertEqual(self.owner, original); self.assertEqual(result["new_concept_ids"], [])
        self.assertEqual(set(self.memory.snapshot()["concepts"]), {"original-product"})
        self.assertEqual(result["seed"]["accepted_matrix"]["totals"]["families"], 1)
        self.assertTrue(result["seed_catalogue_available_to_cycle"])

    def test_one_bounded_branch_per_round_then_choices_freeze_after_finite_first_pass(self):
        rows = [self.prepared(), self.prepared(("Collapsible drying basket",), branch="drying")]
        step = self.step(self.manifest(*rows)); runner = SeedRunner()
        first = self.call(step, runner)
        self.assertEqual(first["seed"]["branches_advanced_this_round"], 1)
        self.assertFalse(first["seed"]["first_pass_done"]); self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.calls, [])
        second = self.call(step, runner)
        self.assertTrue(second["seed"]["first_pass_done"]); self.assertEqual(len(runner.calls), 2)
        self.assertEqual(second["seed"]["counts"]["completed_examples"], 2)
        self.assertGreater(len(self.calls), 0)

    def test_changed_preparation_is_recorded_and_healthy_sibling_still_advances(self):
        bad = self.prepared(); good = self.prepared(("Collapsible drying basket",), branch="drying")
        saved = load_json(Path(bad["prepared_reference"]), 4 * 1024 * 1024); saved["payload"]["items"][0]["material"]["label"] = "Not supplied"
        atomic_json(Path(bad["prepared_reference"]), saved)
        runner = SeedRunner(); step = self.step(self.manifest(bad, good))
        first = self.call(step, runner); second = self.call(step, runner)
        self.assertEqual(first["seed"]["counts"]["actual_judged_examples"], 0)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(second["seed"]["counts"]["accepted_examples"], 1)
        self.assertEqual(second["seed"]["counts"]["pending_examples"], 1)
        self.assertIn("CATALOGUE_SEED_PREPARATION_CHANGED", [row["reason"] for row in second["seed"]["failures"]])
        self.assertGreater(len(self.calls), 0)

    def test_bad_original_material_is_per_item_not_whole_branch_failure(self):
        row = self.prepared(("Magnetic pan lid rack", "Collapsible drying basket"))
        first_id = row["material_ids"][0]
        material_path = self.memory.root / "catalogue_materials/items" / first_id / "material.json"
        saved = load_json(material_path, 4 * 1024 * 1024); saved["receipt_hash"] = "0" * 64; atomic_json(material_path, saved)
        runner = SeedRunner(); result = self.call(self.step(self.manifest(row)), runner)
        self.assertEqual(len(runner.calls), 1); self.assertEqual(len(runner.calls[0]["items"]), 1)
        self.assertEqual(result["seed"]["counts"]["eligible_examples"], 1)
        self.assertEqual(result["seed"]["counts"]["pending_examples"], 1)
        self.assertEqual(result["seed"]["counts"]["accepted_examples"], 1)

    def test_no_useful_and_unclear_are_complete_cached_decisions_not_more_capacity(self):
        row = self.prepared(("Magnetic pan lid rack", "Collapsible drying basket"))
        def decide(value, item):
            if item["material"]["label"] == "Magnetic pan lid rack":
                value.update(decision="no_useful_addition", label="", problem="", distinction="", basis="not_applicable",
                    evidence_ids=[], overlap_ids=[], phrases=[], action_purpose="none")
            else:
                value.update(decision="unclear", phrases=[], action_purpose="none")
        runner = SeedRunner(decide); step = self.step(self.manifest(row))
        first = self.call(step, runner); second = self.call(step, runner, at=LATER)
        self.assertEqual(first["seed"]["state"], "checked")
        self.assertEqual(first["seed"]["counts"]["no_useful_addition_examples"], 1)
        self.assertEqual(first["seed"]["counts"]["unclear_examples"], 1)
        self.assertEqual(first["seed"]["accepted_matrix"]["totals"]["families"], 1)
        self.assertEqual(second["seed"]["branches_advanced_this_round"], 0)
        self.assertEqual(len(runner.calls), 1); self.assertEqual(len(self.calls), 1)

    def test_local_save_failure_reapplies_exact_original_C_answer_after_deferral_without_new_model(self):
        row = self.prepared(); step = self.step(self.manifest(row)); runner = SeedRunner()
        original_put = self.memory.put
        def busy(table, rid, value, **options):
            if table == "catalogue_additions": raise SafetyError("RUNNER_ALREADY_ACTIVE")
            return original_put(table, rid, value, **options)
        with patch.object(self.memory, "put", side_effect=busy):
            first = self.call(step, runner)
        self.assertEqual(first["seed"]["counts"]["pending_examples"], 1)
        second = self.call(step, runner, at=LATER)
        self.assertEqual(second["seed"]["counts"]["pending_examples"], 0)
        self.assertEqual(second["seed"]["counts"]["accepted_examples"], 1)
        self.assertEqual(len(runner.calls), 1)

    def test_existing_frozen_discovery_requests_are_not_rewritten_by_admission(self):
        old = prepare_discovery_cycle(self.memory, self.owner["programme_id"], self.base,
            handlers={"amazon-search": self.handler}, include_lists=False, as_of=NOW)
        row = self.prepared(); result = self.call(self.step(self.manifest(row)), SeedRunner())
        saved = self.memory.snapshot()["batch_runs"][old["id"]]
        self.assertEqual(saved["planned_actions"], old["planned_actions"])
        self.assertEqual(saved["catalogue_hash"], old["catalogue_hash"])
        self.assertEqual(saved["baseline_concepts"], old["baseline_concepts"])
        self.assertFalse(result["seed_catalogue_available_to_cycle"])
        self.assertEqual(result["seed"]["counts"]["accepted_examples"], 1)

    def test_next_programme_uses_accepted_cache_with_no_second_C_call(self):
        row = self.prepared(); manifest = self.manifest(row); runner = SeedRunner()
        self.call(self.step(manifest), runner)
        self.owner["programme_id"] = "next-week"
        result = self.call(self.step(manifest), runner, at=LATER)
        self.assertEqual(result["seed"]["counts"]["cache_reused_examples"], 1)
        self.assertEqual(result["seed"]["counts"]["actual_judged_examples"], 1)
        self.assertEqual(len(runner.calls), 1)

    def test_original_frozen_manifest_is_reused_not_replaced_by_later_preparation(self):
        row = self.prepared(); manifest = self.manifest(row); runner = SeedRunner(); step = self.step(manifest)
        first = self.call(step, runner)
        altered = self.prepared(("Collapsible drying basket",), branch="drying"); self.manifest(altered)
        second = self.call(self.step(manifest), runner)
        self.assertEqual(first["seed"]["seed_cycle_id"], second["seed"]["seed_cycle_id"])
        self.assertEqual(second["seed"]["counts"]["prepared_examples"], 1)
        self.assertEqual(len(runner.calls), 1)

    def test_unprepared_examples_are_not_silently_supplied_or_counted_as_admitted(self):
        row = self.prepared(("Magnetic pan lid rack", "Collapsible drying basket"), limit=1)
        runner = SeedRunner(); result = self.call(self.step(self.manifest(row)), runner)
        self.assertEqual(result["seed"]["counts"]["prepared_examples"], 1)
        self.assertEqual(result["seed"]["counts"]["completed_examples"], 1)
        self.assertEqual(result["seed"]["counts"]["pending_examples"], 1)
        self.assertEqual(len(runner.calls[0]["items"]), 1)

    def test_matrix_is_exact_admitted_file_and_prepared_missing_files_do_not_inflate_it(self):
        row = self.prepared(); runner = SeedRunner(); result = self.call(self.step(self.manifest(row)), runner)
        matrix = result["seed"]["accepted_matrix"]
        saved = load_json(Path(matrix["reference"]), 8 * 1024 * 1024)
        self.assertEqual(digest(saved), matrix["hash"])
        self.assertEqual(saved["matrix"]["totals"]["families"], len(effective_catalogue(self.base, self.memory.snapshot())["families"]))
        self.assertEqual(sum(row["family_phrase_pairs"] for row in saved["matrix"]["categories"].values()), matrix["totals"]["family_phrase_pairs"])
        self.assertEqual(saved["matrix"]["totals"]["source_purpose_options"], len(saved["matrix"]["rows"]))

    def test_preparation_outside_owned_root_is_refused_but_normal_discovery_continues(self):
        row = self.prepared(); row["prepared_reference"] = str(self.root / "outside.json")
        result = self.call(self.step(self.manifest(row)), SeedRunner())
        self.assertEqual(result["seed"]["counts"]["actual_judged_examples"], 0)
        self.assertEqual(result["seed"]["failures"][0]["reason"], "CATALOGUE_SEED_PREPARATION_OUTSIDE_RESEARCH")
        self.assertEqual(len(self.calls), 1)

    def test_malformed_whole_manifest_does_not_stop_connected_free_searches(self):
        result = self.call(self.step({"schema_version": 1, "branches": "not a branch list"}), SeedRunner())
        self.assertEqual(result["seed"]["state"], "input_failed")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result["state"], "work_remaining")

    def test_same_facts_queued_without_runner_do_not_trigger_endless_jobs_or_block_existing_cycle(self):
        row = self.prepared(); step = self.step(self.manifest(row))
        first = self.call(step)
        jobs = deepcopy(self.memory.snapshot()["jobs"])
        second = self.call(step)
        self.assertEqual(self.memory.snapshot()["jobs"], jobs)
        self.assertEqual(first["seed"]["counts"]["actual_judged_examples"], 0)
        self.assertEqual(second["seed"]["branches_advanced_this_round"], 0)
        self.assertEqual(len(self.calls), 1)

    def test_slice_rejects_invalid_bounds_and_duplicate_branches_before_model_calls(self):
        for bound in (False, 0, 11, "1"):
            with self.subTest(bound=bound), self.assertRaisesRegex(SafetyError, "SEED_SLICE_INVALID"):
                self.step({}, seed_branches_per_round=bound)
        row = self.prepared()
        with self.assertRaisesRegex(SafetyError, "MANIFEST_INVALID"):
            prepare_catalogue_seed_cycle(self.memory, "duplicated", self.manifest(row, row, saved=False), as_of=NOW)

    def test_exact_local_merge_is_complete_without_model_and_records_real_eligibility(self):
        row = self.prepared(("Sink organiser",)); result = self.call(self.step(self.manifest(row)))
        self.assertEqual(result["seed"]["counts"]["actual_judged_examples"], 0)
        self.assertEqual(result["seed"]["counts"]["exact_local_merges"], 1)
        self.assertEqual(result["seed"]["counts"]["eligible_examples"], 1)
        self.assertEqual(result["seed"]["state"], "checked")
        self.assertEqual(self.memory.snapshot()["jobs"], {})

    def test_outer_programme_continues_past_exact_merge_and_runs_all_original_lanes(self):
        first = self.prepared(("Sink organiser",)); second = self.prepared(("Collapsible drying basket",), branch="drying")
        step = self.step(self.manifest(first, second)); runner = SeedRunner(); lane_calls = []
        prepare_programme(self.memory, self.owner["programme_id"], source_run_id=self.owner["source_run_id"], as_of=NOW)
        def lane(name):
            def perform(_memory, record, _context):
                lane_calls.append((name, list(record["concept_ids"])))
                return {"state": "nothing_new"}
            return perform
        result = advance_programme(self.memory, self.owner["programme_id"], root=self.root, runner=runner,
            handlers={"amazon-search": self.handler}, steps={name: lane(name) for name in STAGES},
            discovery_step=step, as_of=NOW, max_cycles=4)
        seed = load_programme(self.memory, self.owner["programme_id"])["last_results"]["discovery"]["seed"]
        self.assertEqual(seed["counts"]["completed_examples"], 2)
        self.assertEqual(seed["counts"]["exact_local_merges"], 1)
        self.assertEqual(seed["counts"]["accepted_examples"], 1)
        self.assertEqual(len(runner.calls), 1)
        self.assertTrue(all(ids == ["original-product"] for name, ids in lane_calls))
        self.assertTrue(all(sum(name == expected for name, ids in lane_calls) >= 2 for expected in STAGES))

    def test_outer_programme_does_not_end_after_one_input_failure_before_healthy_seed(self):
        bad = self.prepared(); good = self.prepared(("Collapsible drying basket",), branch="drying")
        bad["payload_hash"] = "0" * 64
        step = self.step(self.manifest(bad, good)); runner = SeedRunner()
        prepare_programme(self.memory, self.owner["programme_id"], source_run_id=self.owner["source_run_id"], as_of=NOW)
        result = advance_programme(self.memory, self.owner["programme_id"], root=self.root, runner=runner,
            handlers={"amazon-search": self.handler}, steps={name: (lambda *_args: {"state": "nothing_new"}) for name in STAGES},
            discovery_step=step, as_of=NOW, max_cycles=4)
        seed = load_programme(self.memory, self.owner["programme_id"])["last_results"]["discovery"]["seed"]
        self.assertTrue(seed["first_pass_done"])
        self.assertEqual(seed["counts"]["accepted_examples"], 1)
        self.assertEqual(seed["counts"]["pending_examples"], 1)
        self.assertEqual(len(runner.calls), 1)

    def test_spoofed_prepared_context_and_candidate_do_not_become_model_facts(self):
        for field in ("context", "candidate"):
            with self.subTest(field=field):
                row = self.prepared(("Magnetic pan lid rack", "Collapsible drying basket"))
                path = Path(row["prepared_reference"]); saved = load_json(path, 4 * 1024 * 1024)
                item = saved["payload"]["items"][0]
                if field == "context": item["material"]["context_excerpt"] = "Invented public observation"
                else: item["candidates"][0]["definition"] = "An invented current catalogue function"
                atomic_json(path, saved); row["payload_hash"] = digest(saved["payload"])
                loaded = load_prepared_catalogue_branch(self.memory, self.base, row)
                runner = SeedRunner()
                result = build_catalogue_branch(self.memory, self.base, runner, branch_id="storage",
                    material_ids=loaded["material_ids"], prepared_payload=loaded["payload"], as_of=NOW)
                self.assertEqual(len(result["pending"]), 1)
                self.assertEqual(len(result["eligible_material_ids"]), 1)
                self.assertFalse(any(item["id"] == row["prepared_material_ids"][0] for call in runner.calls for item in call["items"]))


if __name__ == "__main__":
    unittest.main()
