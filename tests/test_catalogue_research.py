"""Grounded public child categories remain distinct from page/navigation noise."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT / "scripts"))

from catalogue_research import list_child_categories, parse_child_categories, stage_inventory
from product_radar.marketplace_browser import build_marketplace_request, capture_marketplace
from product_radar.models import SafetyError
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal
from test_catalogue_expansion import fixture
from test_marketplace_browser import snapshot, transport

BASE = "https://www.amazon.in/gp/bestsellers/kitchen/1380374031"


class CategoryResearchTests(unittest.TestCase):
    def test_only_actual_navigation_children_are_returned(self):
        raw = '''<html><a href="/gp/bestsellers/kitchen/77/ref=zg_bs_nav_kitchen_5_5">Skip to page</a>
          <div id="zg-left-col"><ul>
          <li><a href="/gp/bestsellers/kitchen/1380374031">This page</a></li>
          <li><a href="/gp/bestsellers/kitchen/55/ref=zg_bs_nav_kitchen_1">Parent</a></li>
          <li><a href="/gp/bestsellers/kitchen/5554155031/ref=zg_bs_nav_kitchen_2_1380374031"><span>Money</span> Boxes</a></li>
          <li><a href="/gp/bestsellers/kitchen/5459540031/ref=zg_bs_nav_kitchen_2_1380374031">Indoor Fountains</a></li>
          <li><a href="https://seller.example/gp/bestsellers/kitchen/123">Not source-owned</a></li>
          </ul></div><a href="/gp/bestsellers/kitchen/123/ref=zg_bs_nav_kitchen_9">Footer</a></html>'''
        rows = parse_child_categories(raw, BASE)
        self.assertEqual([row["label"] for row in rows], ["Money Boxes", "Indoor Fountains"])
        self.assertEqual([row["department"] for row in rows], ["kitchen/5554155031", "kitchen/5459540031"])

    def test_void_nodes_do_not_close_navigation_or_include_footer(self):
        raw = '''<div id="zg_browseRoot"><br/><input disabled><img src="public.jpg"><ul><li>
        <a href="/gp/bestsellers/kitchen/123/ref=zg_bs_nav_kitchen_2">Wind<br/>Chimes</a>
        </li></ul></div><a href="/gp/bestsellers/kitchen/456">Outside</a>'''
        self.assertEqual([row["label"] for row in parse_child_categories(raw, BASE)], ["Wind Chimes"])

    def test_absent_category_navigation_is_no_proof_not_a_fallback_to_every_link(self):
        raw = '<a href="/gp/bestsellers/kitchen/123/ref=zg_bs_nav_kitchen_2">Header</a>'
        self.assertEqual(parse_child_categories(raw, BASE), [])

    def test_verified_original_page_is_required_even_for_readonly_category_export(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); req = build_marketplace_request("amazon-bestsellers", department="kitchen")
            raw = '<div id="zg-left-col"><a href="/gp/bestsellers/kitchen/123/ref=zg_bs_nav_kitchen_1">Tools</a></div>'
            snap = snapshot(req, html=raw)
            capture_marketplace(req, "proof", data_root=root, snapshot_reader=lambda _r: transport(snap))
            actual = list_child_categories(root / "proof")
            self.assertEqual(len(actual["child_categories"]), 1)
            self.assertEqual(actual["model_calls"], 0)
            (root / "proof/rendered.html").write_text(raw + "changed", encoding="utf-8")
            with self.assertRaises(SafetyError):
                list_child_categories(root / "proof")

    def test_inventory_stages_exact_inputs_once_with_zero_canonical_or_model_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); memory = ResearchMemory(root / "memory", Journal(root / "finance"), evidence_type="synthetic")
            req = build_marketplace_request("amazon-bestsellers", department="kitchen")
            capture_root = root / "data/research/public-fixture"
            capture_marketplace(req, "proof", data_root=capture_root,
                snapshot_reader=lambda _r: transport(snapshot(req, text="Sink caddy pack of 2", text_truncated=False)))
            plan = {"schema_version": 1, "id": "inventory-fixture", "entries": [
                {"branch_id": "storage", "source_folder": "data/research/public-fixture/proof",
                 "examples": ["Sink caddy"], "reason": "Inspect the supplied actual functional example."}]}
            before = memory.snapshot()
            first = stage_inventory(memory, fixture(), plan, source_root=root)
            second = stage_inventory(memory, fixture(), plan, source_root=root)
            self.assertEqual(first["state"], "prepared")
            self.assertEqual(first["branches"], second["branches"])
            self.assertEqual(first["material_examples"], 1)
            self.assertEqual(memory.snapshot(), before)
            self.assertEqual(memory.snapshot()["jobs"], {})

    def test_bad_inventory_source_entry_is_recorded_without_stopping_a_healthy_branch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); memory = ResearchMemory(root / "memory", Journal(root / "finance"), evidence_type="synthetic")
            req = build_marketplace_request("amazon-bestsellers", department="kitchen")
            capture_marketplace(req, "proof", data_root=root / "data/research/public-fixture",
                snapshot_reader=lambda _r: transport(snapshot(req, text="Sink caddy pack of 2", text_truncated=False)))
            entry = {"branch_id": "storage", "source_folder": "data/research/public-fixture/proof",
                "examples": ["Sink caddy"], "reason": "Actual example."}
            plan = {"schema_version": 1, "id": "inventory-fixture", "entries": [
                dict(entry, source_folder="other/private-folder"), entry]}
            result = stage_inventory(memory, fixture(), plan, source_root=root)
            self.assertEqual(result["state"], "prepared_with_gaps")
            self.assertEqual(result["prepared_examples"], 1)
            self.assertEqual(len(result["failures"]), 1)
            self.assertEqual(result["failures"][0]["reason"], "CATALOGUE_INVENTORY_SOURCE_OUTSIDE_RESEARCH")
            self.assertEqual(memory.snapshot()["concepts"], {})

    def test_multi_branch_inventory_uses_one_verified_canonical_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            memory = ResearchMemory(root / "memory", Journal(root / "finance"), evidence_type="synthetic")
            catalogue = fixture()
            catalogue["branches"].append({"id": "cleaning", "parent_id": None,
                "category": catalogue["branches"][0]["category"], "name": "Kitchen cleaning"})
            req = build_marketplace_request("amazon-bestsellers", department="kitchen")
            capture_marketplace(req, "proof", data_root=root / "data/research/public-fixture",
                snapshot_reader=lambda _r: transport(snapshot(req,
                    text="Sink caddy. Drain cleaning brush.", text_truncated=False)))
            plan = {"schema_version": 1, "id": "two-branch-fixture", "entries": [
                {"branch_id": "storage", "source_folder": "data/research/public-fixture/proof",
                    "examples": ["Sink caddy"], "reason": "Actual storage example."},
                {"branch_id": "cleaning", "source_folder": "data/research/public-fixture/proof",
                    "examples": ["Drain cleaning brush"], "reason": "Actual cleaning example."}]}
            with patch.object(memory, "snapshot", wraps=memory.snapshot) as snapshots:
                result = stage_inventory(memory, catalogue, plan, source_root=root)
            self.assertEqual(result["prepared_branches"], 2)
            self.assertEqual(snapshots.call_count, 1)
            self.assertEqual(memory.snapshot()["jobs"], {})


if __name__ == "__main__":
    unittest.main()
