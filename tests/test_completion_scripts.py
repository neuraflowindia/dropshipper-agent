"""Saved-work completion commands stop before a batch while the model route waits.

Every store, runner and review call is a local double; no model, provider or
research store is touched.
"""

from contextlib import ExitStack, redirect_stdout
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import complete_practical_reviews as practical
import complete_source_interpretations as interpretations
from product_radar import programme
from product_radar.models import SafetyError


class Runner:
    def __init__(self, *waits):
        self.waits, self.checks = list(waits), 0

    def check_new_execution(self):
        self.checks += 1
        wait = self.waits.pop(0) if self.waits else None
        if wait:
            raise SafetyError(wait)


class CompletionScriptTests(unittest.TestCase):
    def test_both_commands_share_the_programmes_model_route_failures(self):
        self.assertIs(practical.MODEL_ROUTE_FAILURES, programme.MODEL_ROUTE_FAILURES)
        self.assertIs(interpretations.MODEL_ROUTE_FAILURES, programme.MODEL_ROUTE_FAILURES)
        self.assertIn("AI_PAID_CREDITS_USED", practical.MODEL_ROUTE_FAILURES)

    def test_route_wait_names_a_model_route_wait_and_surfaces_anything_else(self):
        self.assertIsNone(practical.route_wait(Runner()))
        self.assertEqual(practical.route_wait(Runner("AI_PLAN_LIMIT_CREDITS_PROTECTED")), "AI_PLAN_LIMIT_CREDITS_PROTECTED")
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
            practical.route_wait(Runner("RESEARCH_EVENTS_INVALID"))

    def run_practical(self, runner, reviews=12):
        memory = MagicMock()
        prepared = [{"review_id": "r" + str(n), "status": "prepared", "reused": False} for n in range(reviews)]
        batches = []

        def review(memory, runner, ids, limit):
            batches.append(list(ids))
            return [{"review_id": i, "status": "accepted"} for i in ids]
        with ExitStack() as stack:
            for name, value in (("ResearchMemory", MagicMock(return_value=memory)), ("Journal", MagicMock()),
                                ("runtime_path", MagicMock()), ("load_json", MagicMock()),
                                ("prepare_practical_fit_reviews", MagicMock(return_value=prepared)),
                                ("research_codex_runner", MagicMock(return_value=runner)),
                                ("run_fit_reviews", MagicMock(side_effect=review)),
                                ("candidate_summary", MagicMock()), ("write_summary", MagicMock())):
                stack.enter_context(patch.object(practical, name, value))
            with redirect_stdout(io.StringIO()):
                code = practical.main(["--live-model"])
        receipt = memory.put.call_args.args[2]
        return code, batches, receipt

    def test_practical_reviews_stop_before_the_next_batch_during_a_money_wait(self):
        code, batches, receipt = self.run_practical(Runner(None, "AI_PAID_CREDITS_USED"))
        self.assertEqual((code, len(batches)), (1, 1))  # the second batch never started
        self.assertEqual((receipt["state"], receipt["reason"]), ("model_route_waiting", "AI_PAID_CREDITS_USED"))
        self.assertEqual(len(receipt["remaining_review_ids"]), 7)
        code, batches, receipt = self.run_practical(Runner("AI_SPEND_WATCH_UNAVAILABLE"))
        self.assertEqual((code, batches, receipt["reason"]), (1, [], "AI_SPEND_WATCH_UNAVAILABLE"))
        code, batches, receipt = self.run_practical(Runner())
        self.assertEqual((code, len(batches), receipt["state"]), (0, 3, "complete"))

    def test_practical_reconciliation_pass_also_stops_during_a_money_wait(self):
        runner = Runner(None, None, "AI_PAID_CREDITS_USED")

        def review(memory, runner, ids, limit):
            return [{"review_id": i, "status": "pending"} for i in ids]
        with patch.object(practical, "run_fit_reviews", side_effect=review) as reviewed:
            memory = MagicMock()
            with ExitStack() as stack:
                for name, value in (("ResearchMemory", MagicMock(return_value=memory)), ("Journal", MagicMock()),
                                    ("runtime_path", MagicMock()), ("load_json", MagicMock()),
                                    ("prepare_practical_fit_reviews", MagicMock(return_value=[
                                        {"review_id": "r" + str(n), "status": "prepared", "reused": False} for n in range(6)])),
                                    ("research_codex_runner", MagicMock(return_value=runner)),
                                    ("candidate_summary", MagicMock()), ("write_summary", MagicMock())):
                    stack.enter_context(patch.object(practical, name, value))
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(practical.main(["--live-model"]), 1)
        self.assertEqual(reviewed.call_count, 2)  # two first-pass batches; no reconciliation batch
        self.assertEqual(memory.put.call_args.args[2]["reason"], "AI_PAID_CREDITS_USED")

    def run_interpretations(self, runner, argv=("--live-model",)):
        memory = MagicMock()
        passes = []

        def review(memory, runner, run_id, skip_targets):
            passes.append(run_id)
            return {"outcomes": [], "reused": [], "remaining_questions": 0}
        questions = MagicMock(return_value=[{"task_id": "t", "concept_id": "c", "unknown_ids": ["x"]}])
        with ExitStack() as stack:
            for name, value in (("ResearchMemory", MagicMock(return_value=memory)), ("Journal", MagicMock()),
                                ("runtime_path", MagicMock()), ("load_json", MagicMock()), ("_questions", questions),
                                ("research_codex_runner", MagicMock(return_value=runner)),
                                ("review_source_results", MagicMock(side_effect=review))):
                stack.enter_context(patch.object(interpretations, name, value))
            with redirect_stdout(io.StringIO()):
                code = interpretations.main(list(argv))
        return code, passes, memory.put.call_args.args[2]

    def test_source_interpretation_waits_before_a_pass_during_a_money_wait(self):
        code, passes, receipt = self.run_interpretations(Runner("AI_PAID_CREDITS_USED"))
        self.assertEqual((code, passes, receipt["reason"]), (1, [], "AI_PAID_CREDITS_USED"))
        code, passes, receipt = self.run_interpretations(Runner())
        self.assertEqual((len(passes), receipt["reason"]), (1, None))
        # Without the live model there is no runner and nothing to check.
        code, passes, receipt = self.run_interpretations(None, argv=())
        self.assertEqual((len(passes), receipt["reason"]), (1, None))


if __name__ == "__main__":
    unittest.main()
