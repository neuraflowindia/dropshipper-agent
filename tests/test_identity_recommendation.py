"""Offline contract/replay tests, not proof of real model identity judgment."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_research_flow import FakeRunner
from product_radar.discovery import find_possible_matches
from product_radar.research_ai import B_PROMPT_VERSION, INSTRUCTIONS, Jobs, NARRATION_PROMPT_VERSION, PROMPT_VERSION, SUPPLIER_PROMPT_VERSION, prompt_version, validate_results
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal


def bookends(label="Bear bookends"):
    return {"label": label, "category": "Home and Decor", "function": "Hold books upright",
            "mechanism": "Fixed side supports", "audience": "People arranging books",
            "features": [label], "basis": "observed", "reason": "Synthetic bookend observation.",
            "brand": "generic_or_small"}


def match_payload():
    return {"items": [{"id": "bird-child", "description": bookends("Bird bookends"),
                       "candidates": [{"id": "bear-concept", "description": bookends()}]}]}


class IdentityRecommendationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.runner = FakeRunner()

    def test_b_only_version_change_preserves_other_roles(self):
        self.assertEqual(B_PROMPT_VERSION, "research-b-functional-v2")
        self.assertEqual(prompt_version("B"), B_PROMPT_VERSION)
        self.assertEqual(prompt_version("A"), PROMPT_VERSION)
        self.assertEqual(prompt_version("E"), PROMPT_VERSION)
        self.assertEqual(PROMPT_VERSION, "research-abe-v1")
        self.assertEqual(prompt_version("F"), SUPPLIER_PROMPT_VERSION)
        self.assertEqual(prompt_version("C"), "research-c-v2")
        self.assertEqual(prompt_version("G"), NARRATION_PROMPT_VERSION)

    def test_identity_instruction_covers_design_splits_and_real_differences(self):
        instruction = INSTRUCTIONS["B"]
        for text in ("useful functional product type", "not a retail SKU", "Decorative motif",
                     "ordinary material substitution alone", "different product URL", "bear, bird",
                     "Folding or adjustable bookends", "Do not merge unrelated decorative objects",
                     "return unclear rather than a forced merge or new concept"):
            self.assertIn(text, instruction)
        self.assertIn("Return a supplied match_id only for same", instruction)

    def test_new_b_binding_does_not_reuse_or_overwrite_old_decision(self):
        jobs, payload = Jobs(self.memory, self.runner), match_payload()
        with patch("product_radar.research_ai.B_PROMPT_VERSION", PROMPT_VERSION):
            old_id, old_result = jobs.run("B", payload)
        old_job = deepcopy(self.memory.snapshot()["jobs"][old_id])
        old_bytes = (self.memory.root / "ai_jobs" / old_id / "result.json").read_bytes()
        new_id, result = jobs.run("B", payload)
        self.assertNotEqual(old_id, new_id)
        self.assertEqual(self.memory.snapshot()["jobs"][old_id], old_job)
        self.assertEqual((self.memory.root / "ai_jobs" / old_id / "result.json").read_bytes(), old_bytes)
        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual(jobs.run("B", payload), (new_id, result))
        self.assertEqual(len(self.runner.calls), 2)
        self.assertEqual(old_result["accepted"]["bird-child"]["decision"], "different")
        # FakeRunner intentionally remains a label-based fixture; this check
        # proves version/cache isolation, not that a real model follows prose.

    def test_same_different_and_unclear_contracts_remain_available(self):
        payload = match_payload()
        for decision, match_id in (("same", "bear-concept"), ("different", None), ("unclear", None)):
            with self.subTest(decision=decision):
                response = {"results": [{"id": "bird-child", "decision": decision,
                            "match_id": match_id, "reason": "Synthetic supported identity judgment."}]}
                result = validate_results("B", payload, response)
                self.assertEqual(result["accepted"]["bird-child"]["decision"], decision)
                self.assertEqual(result["failures"], {})

    def test_merged_and_retired_candidates_do_not_receive_future_matches(self):
        concepts = {status: {"description": bookends(), "status": status}
                    for status in ("provisional", "merged", "retired", "excluded")}
        before = deepcopy(concepts)
        found = find_possible_matches(bookends("Bird bookends"), concepts)
        self.assertEqual({c["id"] for c in found["candidates"]}, {"provisional", "excluded"})
        self.assertEqual(found["omitted"], 0)
        capped = find_possible_matches(bookends(), concepts, limit=1)
        self.assertEqual(len(capped["candidates"]), 1)
        self.assertEqual(capped["omitted"], 1)
        self.assertEqual(concepts, before)

    def test_only_inactive_candidates_cannot_crowd_out_survivor(self):
        concepts = {"inactive-" + str(n): {"description": bookends(), "status": "merged"}
                    for n in range(25)}
        concepts["stable-survivor"] = {"description": bookends(), "status": "provisional"}
        found = find_possible_matches(bookends(), concepts, limit=10)
        self.assertEqual([c["id"] for c in found["candidates"]], ["stable-survivor"])
        self.assertEqual(found["omitted"], 0)

    def test_current_a_job_still_reuses_without_b_version_spillover(self):
        jobs = Jobs(self.memory, self.runner)
        payload = {"items": [{"id": "source1", "source_text": {"title": "Blue sink caddy"}}]}
        first = jobs.run("A", payload)
        with patch("product_radar.research_ai.B_PROMPT_VERSION", "hypothetical-next-b-version"):
            self.assertEqual(jobs.run("A", payload), first)
        self.assertEqual(len(self.runner.calls), 1)

    def test_current_e_job_still_reuses_without_b_version_spillover(self):
        jobs = Jobs(self.memory, self.runner)
        payload = {"items": [{"id": "concept1", "description": bookends(),
                              "observations": [{"id": "obs1", "source_text": {"title": "Bear bookends"}}]}]}
        first = jobs.run("E", payload)
        old_bytes = (self.memory.root / "ai_jobs" / first[0] / "result.json").read_bytes()
        with patch("product_radar.research_ai.B_PROMPT_VERSION", "hypothetical-next-b-version"):
            self.assertEqual(jobs.run("E", payload), first)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual((self.memory.root / "ai_jobs" / first[0] / "result.json").read_bytes(), old_bytes)


if __name__ == "__main__":
    unittest.main()
