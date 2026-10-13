"""Accepted type alternate wording uses actual saved scoped E completion."""

import unittest
from unittest.mock import patch

from product_radar.models import digest
from product_radar.product_queries import SOURCES, ensure_query_profiles
from product_radar.marketplace_browser import capture_marketplace
from product_radar.research_memory import import_direct_marketplace
from product_radar.source_coverage import (create_marketplace_handlers, execute_source_run,
    prepare_source_tasks, product_source_request_builder, source_coverage)
from product_radar.source_interpretation import review_source_results
from test_functional_queries import DEFINITION, Runner as QueryRunner, answer
import test_functional_source_interpretation as source_fixtures
from test_functional_source_interpretation import Runner, NOW
from test_marketplace_browser import snapshot, transport


class TwoQueries(QueryRunner):
    def __call__(self, role, payload, folder):
        values = []
        for item in payload["items"]:
            value = answer(item)
            value["queries"].extend({"source": source, "text": "draining sink holder", "scope": "subtype",
                "reason": "A second explicitly reviewed narrow wording."} for source in SOURCES)
            values.append(value)
        return {"results": values}, {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}


class FunctionalSourcePhraseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = source_fixtures.FunctionalSourceInterpretationTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.memory = self.fixture.memory
        result = ensure_query_profiles(self.memory, TwoQueries(), concept_ids=["first"], as_of=NOW)
        self.assertEqual(result["pending"], [])
        self.builder = product_source_request_builder(run_id="phrase-proof")

    def context(self, *, as_of=NOW):
        state = self.memory.snapshot(); description = state["concepts"]["first"]["description"]
        return {"concept_id": "first", "state": state, "route": "amazon-search", "as_of": as_of,
            "description": description, "description_hash": digest(description), "run_id": "phrase-proof"}

    def acquire(self, *, count=1, empty=False, unrecognised=False):
        context = self.context(); request = self.builder(context)
        planned = prepare_source_tasks(self.memory, [{"route": "amazon-search", "request": request,
            "concept_ids": ["first"], "query_plan": context["query_plan"]}], run_id="phrase-proof", as_of=NOW)
        def capture(request, acquisition_id, **options):
            original = snapshot(request)
            original["cards"] = [dict(original["cards"][0], card_ref=f"card-{n}",
                product_url=f"https://www.amazon.in/dp/B09999999{n}") for n in range(count)] if not (empty or unrecognised) else []
            original.update(card_count=len(original["cards"]), empty=empty)
            return capture_marketplace(request, acquisition_id, **options,
                snapshot_reader=lambda _: transport(original))
        handlers = create_marketplace_handlers(self.memory, capture=capture)
        with patch("product_radar.marketplace_browser.utc_now", return_value=NOW):
            execute_source_run(self.memory, "phrase-proof", handlers=handlers, as_of=NOW,
                bounded_call=lambda callback, context, timeout: callback(context))
        self.task_id = planned["task_ids"][0]
        return request

    def selected(self, *, as_of=NOW):
        context = self.context(as_of=as_of); request = self.builder(context)
        return request["term"], context["query_plan"]

    def review(self, runner=None, **options):
        return review_source_results(self.memory, runner or Runner(relationship="unrelated", judgment="unclear"),
            run_id="phrase-proof", as_of=NOW, **options)

    def test_completed_unrelated_original_advances_without_mutating_legacy_target(self):
        self.acquire(); before = self.memory.snapshot()["query_tasks"][self.task_id]
        self.assertEqual(self.review()["remaining_questions"], 0)
        after = self.memory.snapshot()["query_tasks"][self.task_id]
        self.assertEqual(after, before)
        self.assertEqual(after["target_results"]["first"]["state"], "interpretation_pending")
        panel = next(row for row in source_coverage(self.memory, concept_ids=["first"], as_of=NOW)["rows"]
            if row["route"] == "amazon-search")
        self.assertEqual(panel["state"], "completed_no_results")
        term, plan = self.selected()
        self.assertEqual((term, plan["term_index"], plan["term_count"]), ("draining sink holder", 2, 2))
        self.assertEqual(plan["previous_task_ids"], [self.task_id])

    def test_unclear_complete_original_can_use_second_reviewed_phrase(self):
        self.acquire(); self.review(Runner(relationship="unclear", judgment="unclear"))
        self.assertEqual(self.selected()[0], "draining sink holder")

    def test_any_retained_unreviewed_row_keeps_original_phrase(self):
        self.acquire(count=2)
        self.assertGreater(self.review(per_product=1)["remaining_questions"], 0)
        self.assertEqual(self.selected()[0], "draining sink caddy")
        self.review(per_product=1)
        self.assertEqual(self.selected()[0], "draining sink holder")

    def test_neutral_positive_and_weak_relevant_results_do_not_seek_better_answer(self):
        for judgment in ("neutral", "reasonably_positive", "weak"):
            with self.subTest(judgment=judgment):
                self.fixture.accept(dict(DEFINITION, primary_use="Synthetic meaning " + judgment))
                ensure_query_profiles(self.memory, TwoQueries(), concept_ids=["first"], as_of=NOW)
                self.acquire(); self.review(Runner(judgment=judgment))
                self.assertEqual(self.selected()[0], "draining sink caddy")

    def test_meaning_change_does_not_reuse_old_unrelated_review(self):
        self.acquire(); self.review()
        self.fixture.accept(dict(DEFINITION, primary_use="A newly reviewed ordinary use"))
        ensure_query_profiles(self.memory, TwoQueries(), concept_ids=["first"], as_of=NOW)
        self.assertEqual(self.selected()[0], "draining sink caddy")

    def test_genuine_empty_original_still_advances_without_e_call(self):
        self.acquire(empty=True)
        self.assertEqual(self.selected()[0], "draining sink holder")

    def test_unrecognised_parser_result_is_not_an_empty_wording_answer(self):
        self.acquire(unrecognised=True)
        self.assertEqual(self.memory.snapshot()["query_tasks"][self.task_id]["status"], "insufficient")
        self.assertEqual(self.selected()[0], "draining sink caddy")

    def test_new_global_original_cannot_borrow_earlier_unrelated_interpretation(self):
        request = self.acquire(); self.review()
        self.assertEqual(self.selected()[0], "draining sink holder")
        later = source_fixtures.LATER
        original = snapshot(request)
        original["cards"][0]["product_url"] = "https://www.amazon.in/dp/B099999990"
        folder = self.memory.root / "later-originals"
        with patch("product_radar.marketplace_browser.utc_now", return_value=later):
            capture_marketplace(request, "later-source-original", data_root=folder,
                snapshot_reader=lambda _: transport(original, navigation_started_at=later))
        import_direct_marketplace(self.memory, folder / "later-source-original")
        self.assertEqual(self.selected(as_of=later)[0], "draining sink caddy")


if __name__ == "__main__":
    unittest.main()
