"""Fixture-only saved-analysis fairness; no collection or real model calls."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.models import digest
from product_radar.providers.apify import map_meta_rows
from product_radar.research_flow import _pending_descriptions, run_flow
from product_radar.research_memory import ResearchMemory, import_capture
from product_radar.storage import Journal

NOW = "2026-10-05T10:00:00+00:00"


class EmptyProductRunner:
    identity = "synthetic-analysis-order-v1"

    def __init__(self):
        self.calls = []

    def __call__(self, role, payload, folder):
        assert role == "A", "Uninterpretable fixtures must not create identity or assessment jobs"
        self.calls.append(deepcopy(payload))
        return {"results": [{"id": item["id"], "kind": "uninterpretable", "products": [],
                             "reason": "Synthetic source for saved-analysis scheduling only."}
                            for item in payload["items"]]}, {
            "input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 10}


class AnalysisOrderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")
        self.runner = EmptyProductRunner()

    def capture(self, acquisition, titles):
        rows = [{"ad_archive_id": str(100 + n), "page_id": "200", "is_active": True,
                 "start_date": 1788220800, "snapshot": {"title": title}}
                for n, title in enumerate(titles)]
        import_capture(self.memory, source="meta", acquisition_id=acquisition, observed_at=NOW,
                       mapped=map_meta_rows(rows, purpose="discovery", query_reference=acquisition),
                       capture_hash=digest(rows), reference={"fixture": "synthetic"})

    def titles(self):
        return [[item["source_text"]["title"] for item in payload["items"]]
                for payload in self.runner.calls]

    def run_selected(self, acquisitions, limit):
        return run_flow(self.memory, self.runner, as_of=NOW, acquisition_ids=acquisitions, limit=limit)

    def test_three_captures_limit_three_touches_each_before_second_item(self):
        for acquisition in ("first", "second", "third"):
            self.capture(acquisition, [acquisition + "1", acquisition + "2"])
        self.run_selected(["first", "second", "third"], 3)
        self.assertEqual(self.titles(), [["first1", "second1", "third1"]])

    def test_shared_semantic_consumes_one_slot_and_next_capture_keeps_its_turn(self):
        self.capture("first", ["shared", "first2"])
        self.capture("second", ["shared", "second2"])
        self.capture("third", ["third1", "third2"])
        self.run_selected(["first", "second", "third"], 3)
        self.assertEqual(self.titles(), [["shared", "second2", "third1"]])
        self.assertEqual(len(self.memory.snapshot()["descriptions"]), 3)

    def test_completed_capture_does_not_starve_other_captures(self):
        for acquisition in ("first", "second", "third"):
            self.capture(acquisition, [acquisition + "1", acquisition + "2"])
        self.run_selected(["first"], 2)
        self.run_selected(["first", "second", "third"], 3)
        self.assertEqual(self.titles(), [["first1", "first2"], ["second1", "third1", "second2"]])

    def test_replay_is_stable_then_continuation_processes_every_remaining_item_once(self):
        for acquisition in ("first", "second", "third"):
            self.capture(acquisition, [acquisition + "1", acquisition + "2"])
        state = self.memory.snapshot()
        unique = {o["semantic_id"]: o["semantic"] for o in state["observations"].values()}
        first = _pending_descriptions(state, unique, set(), 3, ["first", "second", "third"])
        self.assertEqual(first, _pending_descriptions(deepcopy(state), unique, set(), 3,
                                                   ["first", "second", "third"]))
        self.run_selected(["first", "second", "third"], 3)
        self.run_selected(["first", "second", "third"], 3)
        before = self.memory.snapshot()
        self.run_selected(["first", "second", "third"], 3)
        self.assertEqual(self.titles(), [["first1", "second1", "third1"],
                                        ["first2", "second2", "third2"]])
        self.assertEqual(self.memory.snapshot(), before)
        self.assertEqual(len(before["descriptions"]), 6)

    def test_single_capture_and_unscoped_processing_keep_original_order(self):
        self.capture("first", ["first1", "first2", "first3"])
        self.capture("second", ["second1", "second2"])
        self.run_selected(["second"], 2)
        run_flow(self.memory, self.runner, as_of=NOW, limit=2)
        self.assertEqual(self.titles(), [["second1", "second2"], ["first1", "first2"]])

    def test_duplicate_selected_capture_does_not_receive_extra_turns(self):
        self.capture("first", ["first1", "first2"])
        self.capture("second", ["second1", "second2"])
        self.run_selected(["first", "first", "second"], 3)
        self.assertEqual(self.titles(), [["first1", "second1", "first2"]])

    def test_deferred_items_stay_pending_without_consuming_a_turn(self):
        self.capture("first", ["first1", "first2"])
        self.capture("second", ["second1", "second2"])
        state = self.memory.snapshot()
        unique = {o["semantic_id"]: o["semantic"] for o in state["observations"].values()}
        deferred = {item_id for item_id, semantic in unique.items() if semantic["title"] == "first1"}
        selected = _pending_descriptions(state, unique, deferred, 3, ["first", "second"])
        self.assertEqual([unique[item_id]["title"] for item_id in selected],
                         ["first2", "second1", "second2"])
        self.assertEqual(len(state["observations"]), 4)


if __name__ == "__main__":
    unittest.main()
