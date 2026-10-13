"""Validation planning is offline and never opens provider/model credentials."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_benchmark_foundation import cli


class ValidationCliTests(unittest.TestCase):
    def test_saved_plan_uses_explicit_inputs_without_model_or_controller(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(cli, "ROOT", Path(temporary)), \
                patch("product_radar.research_operations.plan_meta_validation", return_value={"id": "plan-validation", "actions": [{}]}) as planner, \
                patch("product_radar.research_ai.CodexRunner", side_effect=AssertionError("model forbidden")), \
                patch.object(cli, "MetaTrial", side_effect=AssertionError("provider forbidden")), redirect_stdout(io.StringIO()) as output:
            result = cli.main(["research-validate", "--as-of", "2026-10-06T10:00:00Z", "--slots", "2", "--available-usd", "0.10"])
            self.assertEqual(result, 0)
            self.assertEqual(planner.call_args.kwargs, {"as_of": "2026-10-06T10:00:00Z", "max_actions": 2, "budget_usd": "0.10"})
            self.assertIn("Selected checks: 1", output.getvalue())
            self.assertIn("No collector, model, reservation", output.getvalue())

    def test_default_finite_limit_is_three(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(cli, "ROOT", Path(temporary)), \
                patch("product_radar.research_operations.plan_meta_validation", return_value={"id": "plan-empty", "actions": []}) as planner, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["research-validate"]), 0)
            self.assertEqual(planner.call_args.kwargs["max_actions"], 3)
            self.assertIsNone(planner.call_args.kwargs["budget_usd"])

    def test_invalid_limit_is_refused_before_any_state_write(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(cli, "ROOT", Path(temporary)), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["research-validate", "--slots", "6"]), 2)
            self.assertIn("VALIDATION_PLAN_LIMIT_INVALID", output.getvalue())
            self.assertFalse((Path(temporary) / "data/research/captured/events").exists())

    def test_planning_rejects_execution_and_unrelated_flags_before_memory(self):
        for flags in (["--live"], ["--live-model"], ["--start", "2026-10-13"], ["--acquisition-id", "capture-one"]):
            with self.subTest(flags=flags), patch.object(cli, "Journal", side_effect=AssertionError("state forbidden")), redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["research-validate", *flags]), 2)


if __name__ == "__main__":
    unittest.main()
