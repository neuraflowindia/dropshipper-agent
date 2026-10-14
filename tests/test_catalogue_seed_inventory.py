"""A fresh store prepares its own catalogue seed through the ordinary staging route.

Synthetic local captures only; no website, provider, model or real store.
"""

from datetime import timedelta
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar import catalogue_seed
from product_radar.catalogue_discovery import (SEED_CYCLE_VERSION, _seed_cycle_id, catalogue_seed_lane_hint,
    catalogue_seed_wake_hint, create_catalogue_discovery_step)
from product_radar.catalogue_seed import (INVENTORY_RETRY_SECONDS, load_seed_inventory, prepare_seed_inventory_manifest,
    seed_inventory_gate, seed_inventory_manifest_path)
from product_radar.marketplace_browser import build_marketplace_request, capture_marketplace
from product_radar.models import SafetyError, digest, timestamp
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, atomic_json, load_json
from test_catalogue_expansion import fixture
from test_catalogue_seed import SeedRunner
from test_marketplace_browser import snapshot, transport

NOW = "2026-10-14T10:00:00+00:00"
SOURCE = "data/research/catalogue-public/kitchen-list"


def later(minutes):
    return (timestamp(NOW) + timedelta(minutes=minutes)).isoformat()


class FreshStoreSeedTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="radar-seed-"); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = ResearchMemory(self.root / "data/research/captured",
                                     Journal(self.root / "data/benchmarks/state/apify"), evidence_type="synthetic")
        self.base = fixture()
        self.base["branches"].append(dict(self.base["branches"][0], id="drying", name="Dish drying"))
        self.plan = {"schema_version": 1, "id": "inventory-fixture", "purpose": "Fixture plan.", "entries": [
            {"branch_id": "storage", "source_folder": SOURCE, "examples": ["Magnetic pan lid rack"],
             "reason": "Observed storage function."},
            {"branch_id": "drying", "source_folder": SOURCE, "examples": ["Collapsible drying basket"],
             "reason": "Observed drying function."}]}
        self.owner = {"programme_id": "fresh-programme", "concept_ids": [], "source_run_id": "fresh-source-run"}

    def capture(self, text="Magnetic pan lid rack. Collapsible drying basket."):
        request = build_marketplace_request("amazon-bestsellers", department="kitchen")
        return capture_marketplace(request, "kitchen-list", data_root=self.root / "data/research/catalogue-public",
            snapshot_reader=lambda _request: transport(snapshot(request, text=text, text_truncated=False)))

    def step(self):
        return create_catalogue_discovery_step(self.base, seed_inventory=self.plan,
            seed_source_root=self.root, shared_marketplace=True, renewable_waves=True)

    def call(self, step, runner=None, at=NOW):
        # Renewable waves need an open work turn; they are covered elsewhere.
        # Here they only prove the seed lane never blocks them.
        with patch("product_radar.catalogue_discovery.advance_discovery_waves",
                   return_value={"state": "nothing_new"}) as waves:
            result = step(self.memory, self.owner, {"at": at, "runner": runner, "handlers": {}, "fit_limit": 10})
        self.assertEqual(waves.call_count, 1)
        return result

    def hint(self, step, at=NOW):
        return catalogue_seed_lane_hint(self.memory, self.memory.snapshot(), self.owner["programme_id"], step, as_of=at)

    def manifest_path(self):
        return seed_inventory_manifest_path(self.memory, self.plan)

    def cycle(self):
        return self.memory.snapshot()["batch_runs"].get(_seed_cycle_id(self.owner["programme_id"]))

    def test_fresh_store_prepares_its_own_manifest_then_seed_cycle_through_the_normal_lane(self):
        self.capture(); step = self.step(); runner = SeedRunner()
        self.assertTrue(self.hint(step)["ready"])
        first = self.call(step, runner)
        manifest = load_json(self.manifest_path(), 4 * 1024 * 1024)
        self.assertEqual(manifest["plan_hash"], digest(self.plan))
        self.assertEqual((manifest["prepared_branches"], manifest["prepared_examples"]), (2, 2))
        self.assertEqual((manifest["provider_calls"], manifest["model_calls"], manifest["canonical_writes"]), (0, 0, 0))
        cycle = self.cycle()
        self.assertEqual(cycle["kind"], SEED_CYCLE_VERSION)
        self.assertEqual(cycle["manifest_reference"], str(self.manifest_path()))
        self.assertEqual([row["branch_id"] for row in cycle["planned_branches"]], ["storage", "drying"])
        self.assertEqual(first["seed"]["branches_advanced_this_round"], 1)
        self.assertEqual(first["seed"]["counts"]["actual_judged_examples"], 1)
        self.assertEqual(first["seed"]["paid_actor_calls"], 0)
        self.assertEqual(len(runner.calls), 1)
        # Later turns reuse the store's manifest; staging never repeats.
        with patch.object(catalogue_seed, "stage_catalogue_inventory", side_effect=AssertionError("re-staged")):
            second = self.call(step, runner, at=later(1))
        self.assertTrue(second["seed"]["first_pass_done"])
        self.assertEqual(second["seed"]["counts"]["accepted_examples"], 2)
        self.assertEqual(len(runner.calls), 2)
        self.assertNotEqual(self.hint(step, at=later(2))["signal"].get("manifest"), "missing")

    def test_missing_declared_source_is_a_named_wait_that_never_reports_ready_work(self):
        step = self.step()
        hint = self.hint(step)
        self.assertFalse(hint["ready"]); self.assertIsNone(hint["next_retry_at"])
        with patch.object(catalogue_seed, "stage_catalogue_inventory", side_effect=AssertionError("staged without source")):
            result = self.call(step, SeedRunner())
        self.assertEqual(result["seed"]["state"], "waiting")
        self.assertEqual(result["seed"]["reason"], "CATALOGUE_SEED_SOURCES_UNAVAILABLE")
        self.assertEqual(result["seed"]["missing_sources"], [SOURCE])
        self.assertFalse(self.manifest_path().exists()); self.assertIsNone(self.cycle())
        self.assertEqual(self.memory.snapshot()["jobs"], {})
        # The exact source page appearing changes the signal and wakes the lane.
        self.capture()
        woken = self.hint(step, at=later(1))
        self.assertTrue(woken["ready"]); self.assertNotEqual(woken["signal"], hint["signal"])
        self.call(step, SeedRunner(), at=later(1))
        self.assertTrue(self.manifest_path().is_file()); self.assertIsNotNone(self.cycle())

    def test_unusable_inventory_is_recorded_once_and_waits_for_changed_sources(self):
        self.capture(text="Unrelated public words only."); step = self.step()
        result = self.call(step, SeedRunner())
        self.assertEqual(result["seed"]["state"], "input_failed")
        self.assertEqual(result["seed"]["reason"], "CATALOGUE_SEED_INVENTORY_UNUSABLE")
        self.assertFalse(self.manifest_path().exists()); self.assertIsNone(self.cycle())
        gate = seed_inventory_gate(self.memory, self.plan, source_root=self.root, as_of=later(500))
        self.assertEqual((gate["ready"], gate["reason"], gate["next_retry_at"]), (False, "CATALOGUE_SEED_INVENTORY_UNUSABLE", None))
        self.assertFalse(self.hint(step, at=later(500))["ready"])
        with patch.object(catalogue_seed, "stage_catalogue_inventory", side_effect=AssertionError("repeated work")):
            again = self.call(step, SeedRunner(), at=later(500))
        self.assertEqual(again["seed"]["state"], "waiting")
        # Changed source bytes are a new input: preparation runs again.
        shutil.rmtree(self.root / SOURCE); self.capture()
        self.assertTrue(self.hint(step, at=later(501))["ready"])
        self.call(step, SeedRunner(), at=later(501))
        self.assertTrue(self.manifest_path().is_file())

    def test_local_read_failure_waits_a_bounded_interval_not_every_turn(self):
        self.capture()
        failed = {"branches": [], "failures": [{"entry": 0, "branch_id": "storage", "reason": "CATALOGUE_INVENTORY_MATERIAL_UNREADABLE"}]}
        with patch.object(catalogue_seed, "stage_catalogue_inventory", return_value=failed):
            with self.assertRaisesRegex(SafetyError, "CATALOGUE_SEED_PREPARATION_LOCAL_IO_FAILED"):
                prepare_seed_inventory_manifest(self.memory, self.base, self.plan, source_root=self.root, as_of=NOW)
        waiting = seed_inventory_gate(self.memory, self.plan, source_root=self.root, as_of=later(1))
        self.assertFalse(waiting["ready"])
        self.assertEqual(waiting["next_retry_at"], later(INVENTORY_RETRY_SECONDS // 60))
        self.assertEqual(self.hint(self.step(), at=later(1))["next_retry_at"], waiting["next_retry_at"])
        self.assertTrue(seed_inventory_gate(self.memory, self.plan, source_root=self.root, as_of=later(6))["ready"])
        self.assertFalse(self.manifest_path().exists())

    def test_saved_manifest_must_bind_this_exact_plan(self):
        self.capture()
        path = self.manifest_path()
        atomic_json(path, {"schema_version": 1, "inventory_id": self.plan["id"], "plan_hash": "0" * 64, "branches": []})
        with self.assertRaisesRegex(SafetyError, "CATALOGUE_SEED_MANIFEST_CONFLICT"):
            prepare_seed_inventory_manifest(self.memory, self.base, self.plan, source_root=self.root, as_of=NOW)
        result = self.call(self.step(), SeedRunner())
        self.assertEqual(result["seed"]["reason"], "CATALOGUE_SEED_MANIFEST_CONFLICT")
        self.assertIsNone(self.cycle())

    def test_missing_whole_manifest_file_is_not_ready_work(self):
        absent = self.memory.root / "catalogue_materials/preparations/archived-elsewhere.json"
        hint = catalogue_seed_wake_hint(self.memory, self.memory.snapshot(), "fresh-programme", absent, as_of=NOW)
        self.assertFalse(hint["ready"])
        atomic_json(absent, {"schema_version": 1, "branches": []})
        self.assertTrue(catalogue_seed_wake_hint(self.memory, self.memory.snapshot(), "fresh-programme", absent, as_of=NOW)["ready"])

    def test_options_are_explicit_and_construction_is_read_only(self):
        with self.assertRaisesRegex(SafetyError, "DISCOVERY_SEED_OPTION_INVALID"):
            create_catalogue_discovery_step(self.base, seed_inventory=self.plan)
        with self.assertRaisesRegex(SafetyError, "DISCOVERY_SEED_OPTION_INVALID"):
            create_catalogue_discovery_step(self.base, seed_inventory=self.plan, seed_source_root=self.root,
                                            seed_manifest=self.manifest_path())
        with self.assertRaisesRegex(SafetyError, "CATALOGUE_INVENTORY_INVALID"):
            create_catalogue_discovery_step(self.base, seed_inventory={"schema_version": 1, "id": "x", "entries": []},
                                            seed_source_root=self.root)
        self.step()
        self.assertFalse((self.root / "data/research/captured/catalogue_materials").exists())


class RepositoryInventoryTests(unittest.TestCase):
    def test_repository_inventory_declares_research_sources_under_the_store(self):
        plan = load_seed_inventory()
        self.assertEqual(plan["id"], "public-branch-inventory-20261008")
        folders = {entry["source_folder"] for entry in plan["entries"]}
        self.assertTrue(folders)
        self.assertTrue(all(folder.startswith("data/research/catalogue-public/") for folder in folders))


if __name__ == "__main__":
    unittest.main()
