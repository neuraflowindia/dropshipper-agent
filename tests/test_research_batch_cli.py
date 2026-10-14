"""Saved batch preparation and CLI boundaries; no provider/model calls."""

from contextlib import redirect_stdout
from copy import deepcopy
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.models import SafetyError, prepared_request
from product_radar.research_memory import ResearchMemory
from product_radar.research_operations import plan_research
from product_radar.storage import Journal, atomic_json, load_json
from test_benchmark_foundation import CONFIG, cli
from test_research_planner import NOW, catalogue


class ResearchBatchCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config/benchmarks/meta_first.json"
        atomic_json(self.config, CONFIG)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"))
        self.plan = plan_research(self.memory, catalogue(), as_of=NOW, slots=2)
        self.action = self.plan["actions"][0]
        self.root_patch = patch.object(cli, "ROOT", self.root)
        self.config_patch = patch.object(cli, "CONFIG", self.config)
        self.root_patch.start()
        self.config_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.config_patch.stop)

    def preparation_files(self):
        return sorted((self.root / "data/benchmarks").glob("*/meta-*/prepared.json"))

    def test_brand_review_requires_only_its_saved_input_and_never_live_flags(self):
        cases = [(["research-brand-review"], "BRAND_REVIEW_INPUT_REQUIRED_OR_NOT_ALLOWED"),
                 (["research-summary", "--brand-review-input", "review.json"], "BRAND_REVIEW_INPUT_REQUIRED_OR_NOT_ALLOWED"),
                 (["research-brand-review", "--brand-review-input", "review.json", "--live"], "LIVE_FLAG_NOT_ALLOWED_FOR_OFFLINE_ACTION"),
                 (["research-brand-review", "--brand-review-input", "review.json", "--live-model"], "RESEARCH_FLAG_NOT_ALLOWED")]
        with patch.object(cli, "MetaTrial", side_effect=AssertionError("provider forbidden")), \
                patch("product_radar.research_ai.CodexRunner", side_effect=AssertionError("model forbidden")):
            for arguments, reason in cases:
                with self.subTest(arguments=arguments):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        self.assertEqual(cli.main(arguments), 2)
                    self.assertIn(reason, output.getvalue())

    def test_supplier_capture_requires_explicit_page_access_but_no_model_flag(self):
        cases = [(["research-capture-supplier"], "EXPLICIT_LIVE_REQUIRED"),
                 (["research-capture-supplier", "--live", "--live-model"], "RESEARCH_FLAG_NOT_ALLOWED"),
                 (["research-capture-supplier", "--live", "--item-limit", "2"], "RESEARCH_FLAG_NOT_ALLOWED")]
        with patch.object(cli, "MetaTrial", side_effect=AssertionError("paid provider forbidden")), \
                patch("product_radar.research_ai.CodexRunner", side_effect=AssertionError("model forbidden")):
            for arguments, reason in cases:
                with self.subTest(arguments=arguments):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        self.assertEqual(cli.main(arguments), 2)
                    self.assertIn(reason, output.getvalue())

    def test_supplier_capture_cli_saves_only_known_lead_pages(self):
        atomic_json(self.root / "lead.json", {"concept_id": "known", "lead": {"id": "lead", "urls": ["https://example.com/product"]}})
        saved = {"path": "saved/capture.json", "reused": True,
                 "bundle": {"state": "partial", "item": {"pages": [{}]}, "failures": [{"reason": "PUBLIC_FETCH_DEADLINE"}]}}
        with patch("product_radar.supplier_capture_store.capture_saved_supplier_bundle", return_value=saved) as capture, \
                patch.object(cli, "MetaTrial", side_effect=AssertionError("paid provider forbidden")), \
                patch("product_radar.research_ai.CodexRunner", side_effect=AssertionError("model forbidden")):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli.main(["research-capture-supplier", "--live", "--supplier-input", "lead.json"]), 0)
            self.assertEqual(capture.call_args.args[1]["concept_id"], "known")
            self.assertIn("Usable pages: 1; recorded failures: 1", output.getvalue())
            self.assertIn("No general search, model", output.getvalue())

    def test_supplier_interpretation_cli_carries_original_capture_dates(self):
        atomic_json(self.root / "capture.json", {"bundle": {}})
        atomic_json(self.root / "config/ai/codex_luna.json", {})
        captured = {"item": {"id": "saved-lead", "product": {}, "pages": []},
                    "page_dates": {"page1": "2026-10-01T10:00:00Z"}, "capture_id": "capture-id"}
        with patch("product_radar.supplier_capture_store.load_saved_supplier_input", return_value=captured), \
                patch("product_radar.research_ai.CodexRunner"), \
                patch("product_radar.suppliers.extract_supplier_terms", return_value={"accepted": {}, "failures": {}}) as extract:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["research-suppliers", "--supplier-input", "capture.json", "--live-model"]), 0)
            self.assertEqual(extract.call_args.kwargs["source_dates"], {"saved-lead": captured["page_dates"]})
            self.assertEqual(extract.call_args.args[1], [captured["item"]])

    def test_stale_supplier_envelope_stops_before_model_construction(self):
        atomic_json(self.root / "capture.json", {"bundle": {}})
        with patch("product_radar.supplier_capture_store.load_saved_supplier_input", side_effect=SafetyError("SUPPLIER_CAPTURE_DESCRIPTION_CHANGED")), \
                patch("product_radar.research_ai.CodexRunner", side_effect=AssertionError("model forbidden")):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cli.main(["research-suppliers", "--supplier-input", "capture.json", "--live-model"]), 2)
            self.assertIn("SUPPLIER_CAPTURE_DESCRIPTION_CHANGED", output.getvalue())

    def test_default_production_preparation_is_deterministic_and_offline(self):
        with patch.object(cli, "MetaTrial", side_effect=AssertionError("provider forbidden")):
            first = cli.prepare_planned_action(self.plan, self.action)
            file = self.preparation_files()[0]
            saved_bytes = file.read_bytes()
            second = cli.prepare_planned_action(self.plan, self.action)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.work_scope, "production")
        self.assertEqual(file.read_bytes(), saved_bytes)
        self.assertEqual(len(self.preparation_files()), 1)
        envelope = load_json(file)
        self.assertEqual(envelope["config_hash"], first.config_hash)
        self.assertFalse(envelope["remote_attempt_created"])
        self.assertFalse(envelope["credits_used"])
        trial = prepared_request(cli.prepare({**CONFIG, "term": self.action["request"]["term"],
                                  "purpose": self.action["request"]["purpose"]}, first.attempt_id)["request"])
        self.assertEqual(first.input, trial.input)
        self.assertEqual(first.options, trial.options)
        self.assertNotEqual(first.config_hash, trial.config_hash)

    def test_trial_preparation_retains_legacy_shape_and_conflicts_with_upgrade(self):
        trial = cli.prepare_planned_action(self.plan, self.action, production=False)
        self.assertEqual(trial.work_scope, "trial")
        self.assertNotIn("work_scope", trial.to_dict())
        file = self.preparation_files()[0]
        before = file.read_bytes()
        with self.assertRaisesRegex(SafetyError, "SAVED_PREPARATION_CONFLICT"):
            cli.prepare_planned_action(self.plan, self.action, production=True)
        self.assertEqual(file.read_bytes(), before)

    def test_existing_exact_trial_binding_is_not_silently_upgraded(self):
        original = cli.prepare_planned_action(self.plan, self.action, production=False)
        action = dict(self.action, existing_task={"attempt_id": original.attempt_id,
                                                 "config_hash": original.config_hash})
        before = self.preparation_files()[0].read_bytes()
        # An existing task's saved scope wins even when the new plan would
        # ordinarily use production. No new preparation replaces its trace.
        restored = cli.prepare_planned_action({**self.plan, "id": "another-plan"}, action)
        self.assertEqual(restored.to_dict(), original.to_dict())
        self.assertEqual(restored.work_scope, "trial")
        self.assertEqual(self.preparation_files()[0].read_bytes(), before)
        self.assertEqual(len(self.preparation_files()), 1)

    def test_existing_binding_hash_conflict_leaves_preparation_unchanged(self):
        original = cli.prepare_planned_action(self.plan, self.action)
        action = dict(self.action, existing_task={"attempt_id": original.attempt_id,
                                                 "config_hash": "0" * 64})
        file = self.preparation_files()[0]
        before = file.read_bytes()
        with self.assertRaisesRegex(SafetyError, "SAVED_PREPARATION_CONFLICT"):
            cli.prepare_planned_action(self.plan, action)
        self.assertEqual(file.read_bytes(), before)

    def test_changed_request_cannot_overwrite_same_deterministic_preparation(self):
        cli.prepare_planned_action(self.plan, self.action)
        file = self.preparation_files()[0]
        before = file.read_bytes()
        changed = deepcopy(self.action)
        changed["request"]["term"] = "An entirely different product"
        with self.assertRaisesRegex(SafetyError, "SAVED_PREPARATION_CONFLICT"):
            cli.prepare_planned_action(self.plan, changed)
        self.assertEqual(file.read_bytes(), before)

    def test_batch_rejects_missing_or_unsupported_live_flags_before_access(self):
        cases = [
            (["research-batch"], "EXPLICIT_LIVE_REQUIRED"),
            (["research-batch", "--live"], "EXPLICIT_LIVE_MODEL_REQUIRED"),
            (["research-plan", "--live"], "LIVE_FLAG_NOT_ALLOWED_FOR_OFFLINE_ACTION"),
            (["research-batch", "--live", "--live-model", "--attempt-id", "other"], "ATTEMPT_FLAG_NOT_ALLOWED"),
            (["research-batch", "--live", "--live-model", "--source", "amazon"], "SOURCE_FLAG_NOT_ALLOWED"),
            (["research-batch", "--live", "--live-model", "--production"], "PRODUCTION_FLAG_NOT_ALLOWED"),
            (["research-batch", "--live", "--live-model", "--action-limit", "9"], "BATCH_LIMIT_INVALID"),
            (["research-batch", "--live", "--live-model", "--item-limit", "0"], "RESEARCH_BATCH_LIMIT_INVALID"),
        ]
        with patch.object(cli, "MetaTrial", side_effect=AssertionError("provider forbidden")), \
                patch("product_radar.research_ai.CodexRunner", side_effect=AssertionError("model forbidden")):
            for arguments, reason in cases:
                with self.subTest(arguments=arguments):
                    output = io.StringIO()
                    with redirect_stdout(output):
                        self.assertEqual(cli.main(arguments), 2)
                    self.assertIn(reason, output.getvalue())
        self.assertEqual(self.preparation_files(), [])

    def test_cli_connects_saved_plan_callbacks_with_explicit_live_flags(self):
        atomic_json(self.root / "config/ai/codex_luna.json", {"fixture": "not a real model configuration"})
        model = Mock(name="offline_model_fixture")
        fake_result = {"summary": {"products": []}, "actions": [], "pending_actions": 0, "analysis_reason": None}
        with patch("product_radar.research_batch.execute_plan", return_value=fake_result) as execute, \
                patch("product_radar.research_ai.CodexRunner", return_value=model), \
                patch("product_radar.research_flow.write_summary") as write, \
                patch.object(cli, "MetaTrial") as controller, redirect_stdout(io.StringIO()):
            code = cli.main(["research-batch", "--live", "--live-model", "--plan-id", self.plan["id"],
                             "--action-limit", "2", "--item-limit", "6"])
            self.assertEqual(code, 0)
            kwargs = execute.call_args.kwargs
            self.assertEqual(execute.call_args.args[1], self.plan["id"])
            self.assertIs(kwargs["runner"].runner, model)  # always behind the money guard
            self.assertEqual(kwargs["action_limit"], 2)
            self.assertEqual(kwargs["item_limit"], 6)
            preparation = kwargs["prepare_action"](self.plan, self.action)
            self.assertEqual(preparation.work_scope, "production")
            first_controller = kwargs["controller_factory"]()
            self.assertIs(kwargs["controller_factory"](), first_controller)
            controller.assert_called_once()
            self.assertEqual(controller.call_args.args[0], self.root)
            self.assertTrue(callable(controller.call_args.kwargs["announce"]))
            write.assert_called_once()
        model.assert_not_called()

    def test_named_validation_cli_passes_exact_refinement_without_provider_or_model(self):
        for extra, expected in [([], {}),
                (["--concept-id", "known-product", "--term", "sink organizer", "--reason", "Short public product term."],
                 {"concept_id": "known-product", "term": "sink organizer", "reason": "Short public product term."})]:
            with self.subTest(extra=extra), \
                    patch("product_radar.research_operations.plan_meta_validation", return_value={"id": "saved-plan", "actions": []}) as plan, \
                    patch.object(cli, "MetaTrial", side_effect=AssertionError("provider forbidden")), \
                    patch("product_radar.research_ai.CodexRunner", side_effect=AssertionError("model forbidden")), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["research-validate", "--slots", "1", *extra]), 0)
                self.assertEqual(plan.call_args.kwargs["max_actions"], 1)
                actual = {key: value for key, value in plan.call_args.kwargs.items() if key in ("concept_id", "term", "reason")}
                self.assertEqual(actual, expected)

    def test_named_validation_cli_requires_triplet_and_keeps_other_flags_scoped(self):
        cases = [(["research-validate", "--concept-id", "known"], "META_VALIDATION_REFINEMENT_REQUIRED"),
                 (["research-validate", "--term", "sink organizer"], "META_VALIDATION_REFINEMENT_REQUIRED"),
                 (["research-validate", "--reason", "Shorter query."], "META_VALIDATION_REFINEMENT_REQUIRED"),
                 (["research-validate", "--concept-id", "known", "--term", "sink organizer"], "META_VALIDATION_REFINEMENT_REQUIRED"),
                 (["research-validate", "--concept-id", "known", "--reason", "Shorter query."], "META_VALIDATION_REFINEMENT_REQUIRED"),
                 (["research-validate", "--term", "sink organizer", "--reason", "Shorter query."], "META_VALIDATION_REFINEMENT_REQUIRED"),
                 (["research-validate", "--purpose", "validation"], "QUERY_FLAG_NOT_ALLOWED"),
                 (["research-validate", "--csv", "saved.csv"], "TRENDS_IMPORT_FLAG_NOT_ALLOWED"),
                 (["research-validate", "--receipt", "saved.json"], "TRENDS_IMPORT_FLAG_NOT_ALLOWED"),
                 (["research-summary", "--term", "sink organizer"], "QUERY_FLAG_NOT_ALLOWED"),
                 (["research-summary", "--concept-id", "known"], "TRENDS_IMPORT_FLAG_NOT_ALLOWED"),
                 (["research-summary", "--reason", "Shorter query."], "RESEARCH_RETRY_FLAG_NOT_ALLOWED")]
        with patch("product_radar.research_operations.plan_meta_validation", side_effect=AssertionError("planning forbidden")), \
                patch.object(cli, "MetaTrial", side_effect=AssertionError("provider forbidden")), \
                patch("product_radar.research_ai.CodexRunner", side_effect=AssertionError("model forbidden")):
            for arguments, code in cases:
                with self.subTest(arguments=arguments), redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(cli.main(arguments), 2)
                    self.assertIn(code, output.getvalue())

    def test_offline_continuation_passes_exact_plan_and_reason_without_models(self):
        with patch("product_radar.research_batch.prepare_continuation", return_value=None) as continuation, \
                patch.object(cli, "MetaTrial", side_effect=AssertionError("provider forbidden")), \
                patch("product_radar.research_ai.CodexRunner", side_effect=AssertionError("model forbidden")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["research-continue", "--plan-id", self.plan["id"],
                                       "--reason", "Source receipt repaired."]), 0)
        self.assertEqual(continuation.call_args.args[1], self.plan["id"])
        self.assertEqual(continuation.call_args.kwargs["reason"], "Source receipt repaired.")

    def test_ready_and_allocation_are_offline_and_do_not_reserve(self):
        from test_issue_allocation import products
        from product_radar.fit_policy import ACTIVE_CRITERION_IDS, FIT_POLICY_ID
        eligible = products(25)
        for row in eligible:
            row.update(fit_policy_id=FIT_POLICY_ID, criterion_ids=list(ACTIVE_CRITERION_IDS),
                       kpi_total=10, kpi_matches=6, fit_tier="good",
                       kpi_states=["Meets"] * 6 + ["Unclear"] * 4)
        pool = {"eligible": eligible, "needed_market_work": [], "needed_supplier_work": [], "needed_fact_work": []}
        with patch("product_radar.report_selection.build_report_pool", return_value=pool), \
                patch.object(cli, "MetaTrial", side_effect=AssertionError("provider forbidden")), \
                patch("product_radar.research_ai.CodexRunner", side_effect=AssertionError("model forbidden")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["research-ready"]), 0)
            self.assertEqual(cli.main(["research-allocate", "--start", "2026-10-13", "--end", "2026-10-18"]), 0)
        saved = load_json(self.root / "data/research/captured/report_drafts.json")
        self.assertEqual([len(issue["products"]) for issue in saved["allocation"]["issues"]], [10, 10, 5])
        self.assertTrue(all(issue["client_ready"] is False for issue in saved["allocation"]["issues"]))
        memory = ResearchMemory(self.root / "data/research/captured", Journal(self.root / "data/benchmarks/state/apify"))
        self.assertEqual(memory.snapshot()["issue_reservations"], {})
        self.assertEqual(memory.snapshot()["published_history"], {})

    def test_invalid_allocation_window_does_not_write_pool_or_call_model(self):
        with patch("product_radar.report_selection.build_report_pool", side_effect=AssertionError("invalid input must be rejected first")), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["research-allocate"]), 2)
            self.assertIn("ISSUE_DATE_WINDOW_REQUIRED", output.getvalue())
            self.assertEqual(cli.main(["research-allocate", "--start", "2026-10-11", "--end", "2026-10-12"]), 2)
            self.assertIn("ISSUE_DATE_WINDOW_EMPTY", output.getvalue())
        self.assertFalse((self.root / "data/research/captured/report_pool.json").exists())


if __name__ == "__main__":
    unittest.main()
