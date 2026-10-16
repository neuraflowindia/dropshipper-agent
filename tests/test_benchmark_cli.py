"""Focused public Trends/supplier command boundaries; no real browser or model."""

from contextlib import redirect_stdout
import importlib
import io
from pathlib import Path
import pkgutil
import tempfile
import unittest
from unittest.mock import patch

from test_benchmark_foundation import cli
import product_radar
from product_radar.storage import atomic_bytes, atomic_json, load_json


def setUpModule():
    # These tests patch ``product_radar.storage.load_json`` while the command
    # imports modules lazily. A module first imported under that patch keeps the
    # mock forever through ``from .storage import load_json``, which broke later
    # test modules in the same process. Import every module before any patch.
    for module in pkgutil.walk_packages(product_radar.__path__, "product_radar."):
        importlib.import_module(module.name)


class PublicResearchCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root_patch = patch.object(cli, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def run_cli(self, arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            result = cli.main(arguments)
        return result, output.getvalue()

    def assert_rejected_before_state(self, arguments, reason):
        with patch.object(cli, "Journal", side_effect=AssertionError("state must not be opened")), \
                patch.object(cli, "MetaTrial", side_effect=AssertionError("paid collector forbidden")), \
                patch("product_radar.research_ai.CodexRunner", side_effect=AssertionError("model forbidden")):
            code, output = self.run_cli(arguments)
        self.assertEqual(code, 2)
        self.assertIn(reason, output)
        self.assertNotIn("LOCAL_OPERATION_FAILED", output)

    def test_public_collection_requires_live_but_planning_forbids_it(self):
        self.assert_rejected_before_state(["research-capture-trends", "--check-id", "known"], "EXPLICIT_LIVE_REQUIRED")
        self.assert_rejected_before_state(["research-discover-suppliers", "--concept-id", "known"], "EXPLICIT_LIVE_REQUIRED")
        self.assert_rejected_before_state(["research-plan-trends", "--live"], "LIVE_FLAG_NOT_ALLOWED_FOR_OFFLINE_ACTION")

    def test_current_period_read_settlement_requires_exact_window_and_is_always_offline(self):
        self.assert_rejected_before_state(["settle-renewed-read", "--attempt-id", "saved-run"], "READ_FUNDING_FLAG_REQUIRED_OR_NOT_ALLOWED")
        self.assert_rejected_before_state(["status", "--read-id", "funded-read"], "READ_FUNDING_FLAG_REQUIRED_OR_NOT_ALLOWED")
        self.assert_rejected_before_state(["settle-renewed-read", "--attempt-id", "saved-run", "--read-id", "funded-read", "--live"],
                                          "LIVE_FLAG_NOT_ALLOWED_FOR_OFFLINE_ACTION")
        with patch.object(cli.MetaTrial, "settle_renewed_reads", return_value={"verdict": "Partial", "code": "SYNTHETIC_RECORDED"}) as settle, \
                patch.object(cli, "ApifyTransport", side_effect=AssertionError("network forbidden")):
            code, output = self.run_cli(["settle-renewed-read", "--attempt-id", "saved-run", "--read-id", "funded-read"])
        self.assertEqual(code, 0)
        self.assertIn("SYNTHETIC_RECORDED", output)
        settle.assert_called_once_with("saved-run", "funded-read")

    def test_fit_review_requires_named_bases_and_no_source_collection(self):
        self.assert_rejected_before_state(["research-review-fit", "--review-id", "known"], "EXPLICIT_LIVE_MODEL_REQUIRED")
        self.assert_rejected_before_state(["research-review-fit", "--live-model"], "FIT_REVIEW_IDS_REQUIRED_OR_NOT_ALLOWED")
        self.assert_rejected_before_state(["research-review-fit", "--live-model", "--review-id", "known", "--review-id", "known"], "FIT_REVIEW_BATCH_INVALID")
        self.assert_rejected_before_state(["research-project-fit", "--live-model"], "RESEARCH_FLAG_NOT_ALLOWED")
        self.assert_rejected_before_state(["research-project-fit", "--live"], "LIVE_FLAG_NOT_ALLOWED_FOR_OFFLINE_ACTION")
        self.assert_rejected_before_state(["research-summary", "--review-id", "known"], "FIT_REVIEW_IDS_REQUIRED_OR_NOT_ALLOWED")

    def test_fit_project_and_named_review_have_separate_no_collector_paths(self):
        with patch("product_radar.fit_review.project_saved_fit", return_value={"status": "projected", "applied": True}) as project, \
                patch("product_radar.research_flow.candidate_summary", return_value={"products": []}), \
                patch("product_radar.research_flow.write_summary"), \
                patch("product_radar.research_ai.CodexRunner", side_effect=AssertionError("model forbidden")), \
                patch.object(cli, "MetaTrial", side_effect=AssertionError("collector forbidden")):
            code, output = self.run_cli(["research-project-fit", "--concept-id", "known-product"])
        self.assertEqual(code, 0); self.assertIn("No collector", output)
        project.assert_called_once()
        with patch("product_radar.fit_review.run_fit_reviews", return_value=[]) as review, \
                patch("product_radar.storage.load_json", return_value={"test_config": True}), \
                patch("product_radar.research_ai.CodexRunner"), \
                patch("product_radar.research_flow.candidate_summary", return_value={"products": []}), \
                patch("product_radar.research_flow.write_summary"), \
                patch.object(cli, "MetaTrial", side_effect=AssertionError("collector forbidden")):
            code, output = self.run_cli(["research-review-fit", "--live-model", "--review-id", "known-base"])
        self.assertEqual(code, 0); self.assertEqual(review.call_args.args[2], ["known-base"])

    def test_stale_brand_binding_defers_only_that_projection_candidate(self):
        from product_radar.models import SafetyError
        state = {"concepts": {name: {"id": name, "description": {"label": "Fictional product"}}
                              for name in ("c000", "c001")},
                 "assessments": {name: {} for name in ("c000", "c001")}}
        def verdict(state, kind, concept_id, description):
            if concept_id == "c000":
                raise SafetyError("BRAND_ORIGIN_BINDING_CHANGED")
            return "generic_or_small"
        with patch("product_radar.research_memory.ResearchMemory.snapshot", return_value=state), \
                patch("product_radar.brand_reviews.current_brand", side_effect=verdict), \
                patch("product_radar.fit_review.project_saved_fit", return_value={"status": "projected", "applied": True}) as project, \
                patch("product_radar.research_flow.candidate_summary", return_value={"products": []}), \
                patch("product_radar.research_flow.write_summary"), \
                patch("product_radar.research_ai.CodexRunner", side_effect=AssertionError("model forbidden")), \
                patch.object(cli, "MetaTrial", side_effect=AssertionError("collector forbidden")):
            code, output = self.run_cli(["research-project-fit", "--item-limit", "1"])
        self.assertEqual(code, 0); self.assertIn("BRAND_ORIGIN_BINDING_CHANGED", output)
        self.assertEqual(project.call_args.args[1], "c001"); project.assert_called_once()

    def test_stale_facts_do_not_fill_the_healthy_projection_limit(self):
        state = {"concepts": {name: {"description": {"label": "Fictional product"}} for name in ("c000", "c001")},
                 "assessments": {name: {} for name in ("c000", "c001")}}
        with patch("product_radar.research_memory.ResearchMemory.snapshot", return_value=state), \
                patch("product_radar.brand_reviews.current_brand", return_value="generic_or_small"), \
                patch("product_radar.fit_review.project_saved_fit", side_effect=[
                    {"status": "saved_facts_changed", "applied": False, "review_id": None},
                    {"status": "projected", "applied": True}]) as project, \
                patch("product_radar.research_flow.candidate_summary", return_value={"products": []}), \
                patch("product_radar.research_flow.write_summary"), \
                patch("product_radar.research_ai.CodexRunner", side_effect=AssertionError("model forbidden")):
            code, output = self.run_cli(["research-project-fit", "--item-limit", "1"])
        self.assertEqual(code, 0); self.assertIn("saved_facts_changed", output)
        self.assertEqual([call.args[1] for call in project.call_args_list], ["c000", "c001"])

    def test_checklist_repair_requires_one_exact_saved_failure_and_live_model(self):
        base = ["research-repair-checklist", "--concept-id", "known-product",
                "--failure-id", "known-failure", "--job-id", "known-job"]
        self.assert_rejected_before_state(base, "EXPLICIT_LIVE_MODEL_REQUIRED")
        self.assert_rejected_before_state([*base, "--live-model", "--live"], "LIVE_FLAG_NOT_ALLOWED_FOR_OFFLINE_ACTION")
        self.assert_rejected_before_state([*base, "--live-model", "--item-limit", "2"], "RESEARCH_REPAIR_SINGLE_ITEM_REQUIRED")
        for flags in (["--job-id", "known-job"], ["--concept-id", "../bad", "--failure-id", "known-failure", "--job-id", "known-job"]):
            self.assert_rejected_before_state(["research-repair-checklist", "--live-model", *flags], "INVALID_IDENTIFIER")

    def test_checklist_repair_failure_flag_cannot_leak_into_other_actions(self):
        self.assert_rejected_before_state(["research-summary", "--failure-id", "known-failure"], "CHECKLIST_REPAIR_FLAG_NOT_ALLOWED")

    def test_checklist_repair_calls_only_the_named_product_and_writes_summary(self):
        with patch("product_radar.checklist_repair.repair_checklist_item", return_value={"status": "accepted", "applied": True}) as repair, \
                patch("product_radar.storage.load_json", return_value={"test_config": True}), \
                patch("product_radar.research_ai.CodexRunner") as runner, \
                patch("product_radar.research_flow.candidate_summary", return_value={"products": []}) as summary, \
                patch("product_radar.research_flow.write_summary") as write, \
                patch.object(cli, "MetaTrial", side_effect=AssertionError("paid collector forbidden")):
            code, output = self.run_cli(["research-repair-checklist", "--live-model", "--concept-id", "known-product",
                                         "--failure-id", "known-failure", "--job-id", "known-job"])
        self.assertEqual(code, 0)
        self.assertEqual(repair.call_args.kwargs, {"concept_id": "known-product", "failure_id": "known-failure", "job_id": "known-job"})
        self.assertIs(repair.call_args.args[1].runner, runner.return_value)  # always behind the money guard
        summary.assert_called_once()
        write.assert_called_once()
        self.assertIn("No collector, supplier contact or publication call", output)

    def test_visual_followup_requires_explicit_model_and_saved_input_not_live(self):
        self.assert_rejected_before_state(["research-visual-followup", "--visual-input", "saved.json"], "EXPLICIT_LIVE_MODEL_REQUIRED")
        self.assert_rejected_before_state(["research-visual-followup", "--live-model"], "VISUAL_INPUT_REQUIRED_OR_NOT_ALLOWED")
        self.assert_rejected_before_state(["research-visual-followup", "--live-model", "--visual-input", "saved.json", "--live"],
                                          "LIVE_FLAG_NOT_ALLOWED_FOR_OFFLINE_ACTION")
        self.assert_rejected_before_state(["research-visual-followup", "--live-model", "--visual-input", "saved.json", "--item-limit", "2"],
                                          "VISUAL_SAVED_INPUT_DEFINES_LIMIT")

    def test_visual_input_flag_cannot_leak_into_other_actions(self):
        self.assert_rejected_before_state(["research-summary", "--visual-input", "saved.json"], "VISUAL_INPUT_REQUIRED_OR_NOT_ALLOWED")

    def test_visual_unrelated_flags_fail_before_input_state_or_model(self):
        cases = [(["--check-id", "check"], "TRENDS_CAPTURE_FLAG_NOT_ALLOWED"),
                 (["--browser-path", "/installed/browser"], "TRENDS_CAPTURE_FLAG_NOT_ALLOWED"),
                 (["--supplier-input", "supplier.json"], "SUPPLIER_INPUT_FLAG_NOT_ALLOWED"),
                 (["--concept-id", "product"], "TRENDS_IMPORT_FLAG_NOT_ALLOWED"),
                 (["--job-id", "job"], "RESEARCH_RETRY_FLAG_NOT_ALLOWED"),
                 (["--as-of", "2026-10-06T19:00:00Z"], "PLANNING_FLAG_NOT_ALLOWED")]
        for flags, reason in cases:
            with self.subTest(flags=flags), patch("product_radar.storage.load_json", side_effect=AssertionError("input read forbidden")):
                self.assert_rejected_before_state(["research-visual-followup", "--live-model", "--visual-input", "saved.json", *flags], reason)

    def test_visual_path_must_be_an_existing_nonlinked_project_file(self):
        atomic_json(self.root / "saved.json", [{"child_id": "child", "images": []}])
        (self.root / "linked.json").symlink_to(self.root / "saved.json")
        cases = [("", "VISUAL_INPUT_PATH_INVALID"), ("../outside.json", "VISUAL_INPUT_PATH_INVALID"),
                 ("/tmp/not-this-project.json", "VISUAL_INPUT_PATH_INVALID"),
                 ("missing.json", "SAVED_FILE_UNAVAILABLE"), ("linked.json", "SYMLINK_PATH_REFUSED")]
        for path, reason in cases:
            with self.subTest(path=path):
                self.assert_rejected_before_state(["research-visual-followup", "--live-model", "--visual-input", path], reason)

    def test_visual_input_rejects_malformed_or_oversized_json_before_state(self):
        for raw, reason in ((b"{invalid", "JSON_RESPONSE_INVALID"),
                            (b" " * (256 * 1024 + 1), "SAVED_FILE_SIZE_LIMIT")):
            atomic_bytes(self.root / "saved.json", raw)
            self.assert_rejected_before_state(["research-visual-followup", "--live-model", "--visual-input", "saved.json"], reason)

    def test_visual_requests_check_shape_limits_and_exact_children_before_state(self):
        row = {"child_id": "child", "images": []}
        invalid = [({}, "VISUAL_FOLLOWUP_REQUESTS_INVALID"), ([], "VISUAL_FOLLOWUP_REQUESTS_INVALID"),
                   ([dict(row, child_id="child" + str(i)) for i in range(6)], "VISUAL_FOLLOWUP_REQUESTS_INVALID"),
                   ([dict(row, extra=True)], "VISUAL_FOLLOWUP_REQUESTS_INVALID"),
                   ([{"child_id": "child"}], "VISUAL_FOLLOWUP_REQUESTS_INVALID"),
                   ([dict(row, child_id="../bad")], "INVALID_IDENTIFIER"),
                   ([row, row], "VISUAL_FOLLOWUP_REQUESTS_INVALID"),
                   ([dict(row, images={})], "VISUAL_FOLLOWUP_REQUESTS_INVALID"),
                   ([dict(row, images=[{}] * 5)], "VISUAL_FOLLOWUP_REQUESTS_INVALID"),
                   ([dict(row, images=["not-a-record"])], "VISUAL_FOLLOWUP_REQUESTS_INVALID")]
        for payload, reason in invalid:
            with self.subTest(payload=payload):
                atomic_json(self.root / "saved.json", payload)
                self.assert_rejected_before_state(["research-visual-followup", "--live-model", "--visual-input", "saved.json"], reason)

    def test_visual_followup_passes_exact_saved_requests_and_refreshes_summary(self):
        requests = [{"child_id": "saved-child-" + str(i), "images": [{"saved": "fixture"}] * 4} for i in range(5)]
        atomic_json(self.root / "saved.json", requests)
        result = {"results": [{"child_id": "saved-child-0", "status": "applied", "applied": True}],
                  "applied": 1, "collector_requests": 0, "client_ready": False}
        def read(path, *args):
            return {"fixture_config": True} if path == self.root / "config/ai/codex_luna.json" else load_json(path, *args)
        with patch.object(cli, "Journal") as journal, \
                patch("product_radar.research_memory.ResearchMemory") as memory, \
                patch("product_radar.visual_followups.resolve_saved_visual_followups", return_value=result) as resolve, \
                patch("product_radar.storage.load_json", side_effect=read), \
                patch("product_radar.research_ai.CodexRunner") as runner, \
                patch("product_radar.research_flow.candidate_summary", return_value={"products": []}) as summary, \
                patch("product_radar.research_flow.write_summary") as write, \
                patch.object(cli, "MetaTrial", side_effect=AssertionError("paid collector forbidden")), \
                patch("product_radar.public_pages.fetch_public_page", side_effect=AssertionError("page fetching forbidden")):
            code, output = self.run_cli(["research-visual-followup", "--visual-input", "saved.json", "--live-model"])
        self.assertEqual(code, 0)
        journal.assert_called_once_with(self.root / "data/benchmarks/state/apify")
        self.assertEqual(resolve.call_args.args[:2], (memory.return_value, requests))
        self.assertIs(resolve.call_args.args[2].runner, runner.return_value)  # always behind the money guard
        runner.assert_called_once_with({"fixture_config": True}, stop_on_interrupt=False)
        summary.assert_called_once_with(memory.return_value)
        write.assert_called_once_with(memory.return_value, {"products": []})
        self.assertIn('"collector_requests": 0', output)
        self.assertIn("No photo fetch, collector, supplier contact or publication call", output)
        self.assertFalse((self.root / "data").exists())

    def test_public_commands_do_not_accept_a_model_flag_or_item_limit(self):
        for action in ("research-plan-trends", "research-capture-trends", "research-discover-suppliers"):
            base = [action] + ([] if action == "research-plan-trends" else ["--live"])
            for flags in (["--live-model"], ["--item-limit", "3"]):
                with self.subTest(action=action, flags=flags):
                    self.assert_rejected_before_state([*base, *flags], "RESEARCH_FLAG_NOT_ALLOWED")

    def test_unrelated_flags_rejected_before_any_state_or_network_access(self):
        cases = [(["--attempt-id", "old-attempt"], "ATTEMPT_FLAG_NOT_ALLOWED"),
                 (["--fixture", "synthetic.json"], "FIXTURE_FLAG_NOT_ALLOWED"),
                 (["--purpose", "discovery"], "QUERY_FLAG_NOT_ALLOWED"),
                 (["--source", "trends"], "SOURCE_FLAG_NOT_ALLOWED"),
                 (["--case", "kitchen-movers"], "SOURCE_FLAG_NOT_ALLOWED"),
                 (["--start", "2026-10-13"], "CALENDAR_FLAG_NOT_ALLOWED"),
                 (["--end", "2026-10-18"], "CALENDAR_FLAG_NOT_ALLOWED"),
                 (["--available-usd", "0.10"], "BUDGET_FLAG_NOT_ALLOWED"),
                 (["--slots", "2"], "PLANNING_FLAG_NOT_ALLOWED"),
                 (["--plan-id", "a-plan"], "PLAN_ACTION_FLAG_NOT_ALLOWED"),
                 (["--action-id", "an-action"], "PLAN_ACTION_FLAG_NOT_ALLOWED"),
                 (["--action-limit", "2"], "BATCH_LIMIT_INVALID"),
                 (["--production"], "PRODUCTION_FLAG_NOT_ALLOWED"),
                 (["--csv", "export.csv"], "TRENDS_IMPORT_FLAG_NOT_ALLOWED"),
                 (["--receipt", "receipt.json"], "TRENDS_IMPORT_FLAG_NOT_ALLOWED"),
                 (["--supplier-input", "supplier.json"], "SUPPLIER_INPUT_FLAG_NOT_ALLOWED"),
                 (["--brand-review-input", "brand.json"], "BRAND_REVIEW_INPUT_REQUIRED_OR_NOT_ALLOWED"),
                 (["--acquisition-id", "a-capture"], "ACQUISITION_SCOPE_FLAG_NOT_ALLOWED"),
                 (["--job-id", "a-job"], "RESEARCH_RETRY_FLAG_NOT_ALLOWED"),
                 (["--reason", "Unrelated flag"], "RESEARCH_RETRY_FLAG_NOT_ALLOWED")]
        # Obtain a real allowed case name rather than assuming a provider key.
        cases[4] = (["--case", next(iter(cli.AMAZON_CASES))], "SOURCE_FLAG_NOT_ALLOWED")
        for action in ("research-plan-trends", "research-capture-trends", "research-discover-suppliers"):
            base = [action] + ([] if action == "research-plan-trends" else ["--live"])
            for flags, reason in cases:
                with self.subTest(action=action, flags=flags):
                    self.assert_rejected_before_state([*base, *flags], reason)

    def test_new_flags_remain_scoped_to_their_actual_commands(self):
        cases = [(["research-plan-trends", "--check-id", "known"], "TRENDS_CAPTURE_FLAG_NOT_ALLOWED"),
                 (["research-plan-trends", "--browser-path", "/installed/browser"], "TRENDS_CAPTURE_FLAG_NOT_ALLOWED"),
                 (["research-capture-trends", "--live", "--concept-id", "known"], "TRENDS_IMPORT_FLAG_NOT_ALLOWED"),
                 (["research-capture-trends", "--live", "--term", "food paper"], "QUERY_FLAG_NOT_ALLOWED"),
                 (["research-capture-trends", "--live", "--as-of", "2026-10-06T19:00:00Z"], "PLANNING_FLAG_NOT_ALLOWED"),
                 (["research-discover-suppliers", "--live", "--term", "food paper"], "QUERY_FLAG_NOT_ALLOWED"),
                 (["research-discover-suppliers", "--live", "--check-id", "known"], "TRENDS_CAPTURE_FLAG_NOT_ALLOWED"),
                 (["research-discover-suppliers", "--live", "--as-of", "2026-10-06T19:00:00Z"], "PLANNING_FLAG_NOT_ALLOWED"),
                 (["research-summary", "--check-id", "known"], "TRENDS_CAPTURE_FLAG_NOT_ALLOWED"),
                 (["research-summary", "--browser-path", "/installed/browser"], "TRENDS_CAPTURE_FLAG_NOT_ALLOWED")]
        for arguments, reason in cases:
            with self.subTest(arguments=arguments):
                self.assert_rejected_before_state(arguments, reason)

    def test_plan_trends_passes_exact_product_term_and_time_without_browser(self):
        with patch("product_radar.market_followups.prepare_trends_check", return_value={"id": "trends-check-test"}) as prepare, \
                patch("product_radar.trends_capture.capture", side_effect=AssertionError("browser forbidden")), \
                patch.object(cli, "MetaTrial", side_effect=AssertionError("paid collector forbidden")), \
                patch("product_radar.research_ai.CodexRunner", side_effect=AssertionError("model forbidden")):
            code, output = self.run_cli(["research-plan-trends", "--concept-id", "known-product", "--term", "food wrap paper",
                                          "--as-of", "2026-10-06T19:00:00Z"])
        self.assertEqual(code, 0)
        self.assertEqual(prepare.call_args.kwargs, {"concept_id": "known-product", "term": "food wrap paper", "as_of": "2026-10-06T19:00:00Z"})
        self.assertIn("Saved useful Trends check: trends-check-test", output)
        self.assertIn("No browser, model or paid collector", output)

    def test_plan_trends_missing_or_invalid_concept_and_term_fail_before_reading_memory(self):
        for flags, reason in (([], "INVALID_IDENTIFIER"), (["--concept-id", "../bad", "--term", "food paper"], "INVALID_IDENTIFIER"),
                              (["--concept-id", "known", "--term", " "], "TRENDS_TERM_INVALID"),
                              (["--concept-id", "known", "--term", "x" * 121], "TRENDS_TERM_INVALID"),
                              (["--concept-id", "known", "--term", "food\npaper"], "TRENDS_TERM_INVALID")):
            with self.subTest(flags=flags), patch("product_radar.research_memory.ResearchMemory.snapshot", side_effect=AssertionError("invalid input must not read memory")):
                code, output = self.run_cli(["research-plan-trends", *flags])
                self.assertEqual(code, 2)
                self.assertIn(reason, output)
                self.assertNotIn("LOCAL_OPERATION_FAILED", output)

    def test_plan_trends_without_operator_term_uses_shared_family_profile_default(self):
        with patch("product_radar.market_followups.prepare_trends_check", return_value={"id": "compact-family"}) as prepare, \
                patch.object(cli, "MetaTrial", side_effect=AssertionError("paid collector forbidden")), \
                patch("product_radar.research_ai.CodexRunner", side_effect=AssertionError("model forbidden")):
            code, output = self.run_cli(["research-plan-trends", "--concept-id", "known"])
        self.assertEqual(code, 0)
        self.assertIsNone(prepare.call_args.kwargs["term"])
        self.assertIn("compact-family", output)

    def test_capture_trends_missing_or_invalid_check_id_never_loads_a_request(self):
        for flags in ([], ["--check-id", "../bad"]):
            with self.subTest(flags=flags), patch("product_radar.storage.load_json", side_effect=AssertionError("request load forbidden")), \
                    patch("product_radar.market_followups.execute_trends_check", side_effect=AssertionError("capture forbidden")):
                code, output = self.run_cli(["research-capture-trends", "--live", *flags])
                self.assertEqual(code, 2)
                self.assertIn("INVALID_IDENTIFIER", output)

    def test_capture_trends_uses_exact_named_request_and_browser_path_without_model(self):
        request = {"id": "known-check", "concept_id": "known-product", "term": "food paper"}
        result = {"state": "Blocked", "request_id": "known-check", "client_ready": False}
        with patch("product_radar.storage.load_json", return_value=request) as read, \
                patch("product_radar.market_followups.execute_trends_check", return_value=result) as execute, \
                patch("product_radar.research_flow.candidate_summary", return_value={"products": []}), \
                patch("product_radar.research_flow.write_summary") as summary, \
                patch.object(cli, "MetaTrial", side_effect=AssertionError("paid collector forbidden")), \
                patch("product_radar.research_ai.CodexRunner", side_effect=AssertionError("model forbidden")):
            code, output = self.run_cli(["research-capture-trends", "--live", "--check-id", "known-check", "--browser-path", "/installed/browser"])
        self.assertEqual(code, 0)
        self.assertEqual(read.call_args.args[0], self.root / "data/research/captured/trends_checks/known-check/request.json")
        self.assertEqual(execute.call_args.args[1], request)
        self.assertEqual(execute.call_args.kwargs["browser_path"], "/installed/browser")
        self.assertEqual(execute.call_args.kwargs["root"], self.root)
        self.assertTrue(callable(execute.call_args.kwargs["capture"]))
        summary.assert_called_once()
        self.assertIn('"state": "Blocked"', output)
        self.assertIn("No Apify, model or publication call", output)

    def test_capture_trends_refuses_request_with_a_different_saved_id(self):
        for request in ({"id": "another-check"}, {}, None, []):
            with self.subTest(request=request), patch("product_radar.storage.load_json", return_value=request), \
                    patch("product_radar.market_followups.execute_trends_check") as execute, \
                    patch("product_radar.research_flow.candidate_summary", return_value={"products": []}), \
                    patch("product_radar.research_flow.write_summary"):
                code, output = self.run_cli(["research-capture-trends", "--live", "--check-id", "requested-check"])
            self.assertEqual(code, 2)
            self.assertIn("TRENDS_CHECK_ID_MISMATCH", output)
            execute.assert_not_called()

    def test_supplier_discovery_missing_or_invalid_concept_never_opens_browser(self):
        for flags in ([], ["--concept-id", "../bad"]):
            with self.subTest(flags=flags), patch("product_radar.supplier_discovery.discover_saved_supplier_leads", side_effect=AssertionError("browser forbidden")):
                code, output = self.run_cli(["research-discover-suppliers", "--live", *flags])
                self.assertEqual(code, 2)
                self.assertIn("INVALID_IDENTIFIER", output)

    def test_supplier_discovery_connects_browser_search_destination_capture_and_next_inputs(self):
        search = {"search_id": "saved-supplier-search", "result": {"state": "complete", "candidate_count": 3}}
        check = {"source": "supplier", "route": "google-browser-html", "at": "2026-10-06T19:00:00Z", "concept_id": "known-product", "state": "insufficient"}
        destinations = {"pending_interpretation_paths": ["saved/one/capture.json", "saved/two/capture.json"], "client_ready": False}
        with patch("product_radar.supplier_discovery.discover_saved_supplier_leads", return_value=search) as discover, \
                patch("product_radar.supplier_discovery.supplier_search_check", return_value=check), \
                patch("product_radar.supplier_discovery.capture_discovered_supplier_pages", return_value=destinations) as capture, \
                patch("product_radar.supplier_search_browser.fetch_google_search_page", return_value={"fixture": True}) as browser, \
                patch.object(cli, "MetaTrial", side_effect=AssertionError("paid collector forbidden")), \
                patch("product_radar.research_ai.CodexRunner", side_effect=AssertionError("model forbidden")):
            code, output = self.run_cli(["research-discover-suppliers", "--live", "--concept-id", "known-product", "--browser-path", "/installed/browser"])
            self.assertEqual(code, 0)
            self.assertEqual(discover.call_args.args[1], "known-product")
            self.assertEqual(discover.call_args.kwargs["route"], "google-browser-html")
            item = {"id": "query", "url": "https://www.google.com/search"}
            discover.call_args.kwargs["fetcher"](item)
            browser.assert_called_once_with(item, browser_path="/installed/browser")
        self.assertEqual(capture.call_args.args[1], search)
        self.assertEqual(load_json(self.root / "data/research/captured/supplier_search_next.json"), destinations)
        self.assertIn("candidate pages: 3", output)
        self.assertIn("awaiting interpretation: 2", output)
        self.assertIn("No model call, paid collector, supplier contact or publication", output)


if __name__ == "__main__":
    unittest.main()
