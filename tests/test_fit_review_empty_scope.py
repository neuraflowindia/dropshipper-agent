"""Explicit empty fit preparation is a validated, read-free local no-op."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import product_radar.fit_review as fit_review
from product_radar.fit_description import DESCRIPTION_CONTRACT, FUNCTIONAL_CONTRACT
from product_radar.models import SafetyError
import test_fit_review as fit_fixtures
import test_programme_fit_recovery as programme_fixtures
from test_research_flow import NOW


class EmptyFitPreparationTests(unittest.TestCase):
    def untouched_memory(self):
        return SimpleNamespace(
            snapshot=Mock(side_effect=AssertionError("Empty targets reread saved research")),
            commit=Mock(side_effect=AssertionError("Empty targets wrote saved research")),
            put=Mock(side_effect=AssertionError("Empty targets wrote a saved record")),
        )

    def test_explicit_empty_list_returns_exact_empty_result_without_io_or_model_calls(self):
        memory = self.untouched_memory()
        with patch("product_radar.research_ai.Jobs.run", side_effect=AssertionError("Empty targets ran a model")) as model:
            for contract in (fit_review.PRACTICAL_CONTRACT, DESCRIPTION_CONTRACT, FUNCTIONAL_CONTRACT):
                with self.subTest(contract=contract):
                    result = fit_review.prepare_practical_fit_reviews(memory, concept_ids=[],
                        as_of=NOW, contract_version=contract)
                    self.assertIs(type(result), list)
                    self.assertEqual(result, [])
        memory.snapshot.assert_not_called()
        memory.commit.assert_not_called()
        memory.put.assert_not_called()
        model.assert_not_called()

    def test_empty_list_still_validates_the_contract_before_any_io(self):
        memory = self.untouched_memory()
        with self.assertRaisesRegex(SafetyError, "FIT_REVIEW_REQUEST_INVALID"):
            fit_review.prepare_practical_fit_reviews(memory, concept_ids=[], as_of=NOW,
                contract_version="unrecognised-contract")
        memory.snapshot.assert_not_called()
        memory.commit.assert_not_called()

    def test_empty_list_still_validates_an_explicit_date_before_any_io(self):
        memory = self.untouched_memory()
        with self.assertRaises(SafetyError):
            fit_review.prepare_practical_fit_reviews(memory, concept_ids=[], as_of="not-a-date")
        memory.snapshot.assert_not_called()
        memory.commit.assert_not_called()

    def test_empty_list_still_validates_the_default_clock(self):
        memory = self.untouched_memory()
        with patch.object(fit_review, "utc_now", return_value="not-a-date") as clock:
            with self.assertRaises(SafetyError):
                fit_review.prepare_practical_fit_reviews(memory, concept_ids=[])
        clock.assert_called_once_with()
        memory.snapshot.assert_not_called()
        memory.commit.assert_not_called()

    def test_empty_tuple_remains_invalid_and_does_not_take_the_list_fast_path(self):
        memory = SimpleNamespace(snapshot=Mock(return_value={"concepts": {}}), commit=Mock())
        with self.assertRaisesRegex(SafetyError, "FIT_REVIEW_REQUEST_INVALID"):
            fit_review.prepare_practical_fit_reviews(memory, concept_ids=(), as_of=NOW)
        memory.snapshot.assert_called_once_with()
        memory.commit.assert_not_called()

    def test_none_and_nonempty_list_still_prepare_and_reuse_actual_current_reviews(self):
        fixture = fit_fixtures.FitReviewTests(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        memory = fixture.memory
        with patch.object(memory, "snapshot", wraps=memory.snapshot) as reads:
            all_rows = fit_review.prepare_practical_fit_reviews(memory, concept_ids=None, as_of=NOW)
        self.assertGreaterEqual(reads.call_count, 1)
        self.assertEqual([row["concept_id"] for row in all_rows], sorted(memory.snapshot()["concepts"]))
        self.assertTrue(all(row["status"] == "prepared" for row in all_rows))
        before = memory.snapshot_with_provenance()
        explicit = fit_review.prepare_practical_fit_reviews(memory, concept_ids=[fixture.concept_id], as_of=NOW)
        self.assertEqual(explicit, [row for row in all_rows if row["concept_id"] == fixture.concept_id])
        self.assertEqual(memory.snapshot_with_provenance(), before)
        runner = fit_fixtures.FitRunner()
        fit_review.run_fit_reviews(memory, runner, [row["review_id"] for row in explicit])
        before = memory.snapshot_with_provenance()
        reused = fit_review.prepare_practical_fit_reviews(memory, concept_ids=[fixture.concept_id], as_of=NOW)
        self.assertEqual(len(reused), 1)
        self.assertEqual(reused[0]["status"], "accepted")
        self.assertTrue(reused[0]["reused"])
        self.assertEqual(memory.snapshot_with_provenance(), before)
        self.assertEqual(len(runner.calls), 1)

    def test_current_fit_programme_scope_does_not_read_history_for_empty_preparation(self):
        fixture = programme_fixtures.ProgrammeFitRecoveryTests(); fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        bases = fixture.prepare_v2()
        fit_review.run_fit_reviews(fixture.memory, fit_fixtures.FitRunner(),
            [row["review_id"] for row in bases])
        calls = []
        real = fit_review.prepare_practical_fit_reviews

        def traced(memory, **kwargs):
            with patch.object(memory, "snapshot", wraps=memory.snapshot) as reads:
                result = real(memory, **kwargs)
            calls.append({"concept_ids": kwargs.get("concept_ids"), "rows": result,
                          "snapshot_calls": reads.call_count})
            return result

        before = fixture.memory.snapshot_with_provenance()
        with patch.object(fit_review, "prepare_practical_fit_reviews", side_effect=traced):
            result, record = fixture.stage()
        self.assertEqual(result["current_accepted_reused"], 1)
        self.assertEqual(calls, [{"concept_ids": [], "rows": [], "snapshot_calls": 0}])
        self.assertEqual(record["deferrals"], {})
        self.assertEqual(fixture.memory.snapshot_with_provenance(), before)


if __name__ == "__main__":
    unittest.main()
