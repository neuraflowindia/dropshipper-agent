"""Local CLI fixtures use synthetic facts and never contact models/browsers."""

from contextlib import redirect_stdout
from copy import deepcopy
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
from product_radar.models import digest
from product_radar.research_memory import ResearchMemory
from product_radar.storage import Journal, atomic_json, load_json
from test_live_outputs import fixture
from test_report_narration import NarrationRunner
import test_report_workflow as workflow_fixture

spec = importlib.util.spec_from_file_location("build_review_package_cli_fixture", ROOT / "scripts/build_review_package.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class BuildReviewPackageCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.issue, dataset = fixture()
        self.pool = {"evidence_type": "captured", "product_facts": deepcopy(dataset["products"])}
        self.drafts = {"pool_hash": digest(self.pool), "allocation": {"issues": [self.issue]}}
        self.folder = self.root / "data/research/captured"
        self.save()
        atomic_json(self.root / "config/ai/codex_luna.json", {"fixture": "synthetic"})
        self.enterContext(patch.object(cli, "ROOT", self.root))
        self.policy = self.enterContext(patch.object(cli, "load_policy", return_value={"ongoing_pdf_language": None}))

    def save(self, *, update_hash=True):
        if update_hash:
            self.drafts["pool_hash"] = digest(self.pool)
        atomic_json(self.folder / "report_pool.json", self.pool)
        atomic_json(self.folder / "report_drafts.json", self.drafts)

    def run_cli(self, *, live=True, issue_id=None, presentation_source_revision=None):
        out = io.StringIO()
        args = ["--issue-id", issue_id or self.issue["issue_id"]]
        if live:
            args.append("--live-model")
        if presentation_source_revision is not None:
            args.extend(("--presentation-source-revision", str(presentation_source_revision)))
        with redirect_stdout(out):
            status = cli.main(args)
        return status, out.getvalue()

    def test_live_flag_required_before_reading_memory_or_creating_runner(self):
        with patch.object(cli, "ResearchMemory") as memory, patch.object(cli, "research_codex_runner") as runner:
            status, output = self.run_cli(live=False)
        self.assertEqual(status, 2)
        self.assertIn("EXPLICIT_LIVE_MODEL_REQUIRED", output)
        memory.assert_not_called()
        runner.assert_not_called()

    def test_missing_saved_drafts_or_pool_does_not_initialize_runner(self):
        for filename in ("report_drafts.json", "report_pool.json"):
            with self.subTest(filename=filename):
                self.save()
                (self.folder / filename).unlink()
                with patch.object(cli, "research_codex_runner") as runner:
                    status, output = self.run_cli()
                self.assertEqual(status, 2)
                self.assertIn("SAVED_FILE_UNAVAILABLE", output)
                runner.assert_not_called()

    def test_changed_pool_or_synthetic_namespace_is_refused_before_runner(self):
        for kind in ("changed", "synthetic"):
            with self.subTest(kind=kind):
                if kind == "changed":
                    self.pool["product_facts"][0]["title"] = "Changed fixture"
                    self.save(update_hash=False)
                else:
                    self.pool["evidence_type"] = "synthetic"
                    self.save()
                with patch.object(cli, "research_codex_runner") as runner:
                    status, output = self.run_cli()
                self.assertEqual(status, 2)
                self.assertIn("SAVED_REPORT_POOL_CHANGED", output)
                runner.assert_not_called()

    def test_unknown_or_duplicate_saved_issue_is_refused_before_runner(self):
        with patch.object(cli, "research_codex_runner") as runner:
            status, output = self.run_cli(issue_id="unknown-issue")
        self.assertEqual(status, 2)
        self.assertIn("SAVED_REPORT_ISSUE_REQUIRED", output)
        runner.assert_not_called()
        self.drafts["allocation"]["issues"].append(deepcopy(self.issue))
        self.save()
        with patch.object(cli, "research_codex_runner") as runner:
            status, output = self.run_cli()
        self.assertEqual(status, 2)
        self.assertIn("SAVED_REPORT_ISSUE_REQUIRED", output)
        runner.assert_not_called()

    def test_missing_duplicate_or_wrong_selection_facts_refused_without_model_work(self):
        original = deepcopy(self.pool["product_facts"])
        for kind in ("missing", "duplicate", "selection_changed"):
            with self.subTest(kind=kind):
                self.pool["product_facts"] = deepcopy(original)
                if kind == "missing":
                    self.pool["product_facts"].pop()
                elif kind == "duplicate":
                    self.pool["product_facts"].append(deepcopy(original[0]))
                else:
                    self.issue["selection_hash"] = "a" * 64
                self.save()
                work = Mock(side_effect=AssertionError("Model invocation forbidden"))
                with patch.object(cli, "research_codex_runner", return_value=work), patch.object(cli, "render") as render:
                    status, output = self.run_cli()
                self.assertEqual(status, 2)
                expected = {"missing": "LIVE_FACTS_CONCEPT_MISMATCH", "duplicate": "LIVE_DUPLICATE_CONCEPT",
                            "selection_changed": "LIVE_ISSUE_FACTS_CHANGED"}[kind]
                self.assertIn(expected, output)
                work.assert_not_called()
                render.assert_not_called()

    def test_shortfall_returns_without_runner_or_renderer(self):
        self.issue.update(ready_for_review=False, shortfall=3)
        self.save()
        with patch.object(cli, "research_codex_runner") as runner, patch.object(cli, "render") as render:
            status, output = self.run_cli()
        self.assertEqual(status, 0)
        self.assertIn("state: shortfall", output)
        self.assertIn("needed: 3", output)
        runner.assert_not_called()
        render.assert_not_called()

    def test_later_language_pending_saves_binding_without_runner_or_renderer(self):
        self.issue.update(projected_ordinal=3, language_state="language_pending")
        self.save()
        with patch.object(cli, "research_codex_runner") as runner, patch.object(cli, "render") as render:
            status, output = self.run_cli()
        self.assertEqual(status, 0)
        self.assertIn("state: language_pending", output)
        runner.assert_not_called()
        render.assert_not_called()
        frozen = load_json(self.root / "data/outputs" / self.issue["issue_id"] / "revision-1/package-input.json", 8 * 1024 * 1024)
        self.assertEqual(frozen["issue"], self.issue)
        self.assertEqual(frozen["product_facts"], self.pool["product_facts"])

    def test_cli_passes_only_exact_issue_facts_and_explicit_selected_language(self):
        other = dict(self.pool["product_facts"][0], concept_id="other-product")
        self.pool["product_facts"] = [other, *reversed(self.pool["product_facts"])]
        self.policy.return_value = {"ongoing_pdf_language": "en"}
        self.save()
        runner = Mock()
        with patch.object(cli, "research_codex_runner", return_value=runner), \
                patch.object(cli, "prepare_review_package", return_value={"state": "draft_ready_for_review", "manifest_path": "fixture-only"}) as prepare:
            status, output = self.run_cli()
        self.assertEqual(status, 0)
        args, options = prepare.call_args
        self.assertEqual(args[1], self.issue)
        self.assertEqual({row["concept_id"] for row in args[2]}, {row["concept_id"] for row in self.issue["products"]})
        self.assertEqual(len(args[2]), len(self.issue["products"]))
        self.assertIs(options["runner"], runner)
        self.assertEqual(options["selected_language"], "en")
        self.assertIs(options["render_pdf"], cli.render)
        self.assertIn("No collection, reservation, approval", output)

    def test_presentation_cli_does_not_initialize_a_runner_or_read_model_config(self):
        (self.root / "config/ai/codex_luna.json").unlink()
        with patch.object(cli, "research_codex_runner") as runner, \
                patch.object(cli, "prepare_review_package", return_value={"state": "draft_ready_for_review"}) as prepare:
            status, output = self.run_cli(live=False, presentation_source_revision=1)
        self.assertEqual(status, 0)
        runner.assert_not_called()
        self.assertIsNone(prepare.call_args.kwargs["runner"])
        self.assertEqual(prepare.call_args.kwargs["presentation_source_revision"], 1)
        self.assertIn("No collection, reservation, approval", output)

    def test_invalid_presentation_source_revision_is_refused_before_memory_or_runner(self):
        with patch.object(cli, "ResearchMemory") as memory, patch.object(cli, "research_codex_runner") as runner:
            status, output = self.run_cli(live=False, presentation_source_revision=0)
        self.assertEqual(status, 2)
        self.assertIn("REPORT_PRESENTATION_REVISION_INVALID", output)
        memory.assert_not_called()
        runner.assert_not_called()

    def test_injected_synthetic_runner_and_renderer_create_unapproved_pair(self):
        # The CLI's saved-file checks are exercised above. This connection test
        # injects a synthetic namespace explicitly so fictional facts never
        # become captured research or an actual client-approved package.
        memory = ResearchMemory(self.root / "fixture-research", Journal(self.root / "fixture-finance"), evidence_type="synthetic")
        runner = NarrationRunner(memory=memory)
        render_fixture = workflow_fixture.WorkflowTests("test_complete_pair_connects_once_and_replays_without_model_or_browser")
        render_fixture.renders = []
        with patch.object(cli, "ResearchMemory", return_value=memory), \
                patch.object(cli, "load_json", side_effect=lambda path, *args: self.drafts if Path(path).name == "report_drafts.json" else self.pool if Path(path).name == "report_pool.json" else {}), \
                patch.object(cli, "research_codex_runner", return_value=runner), \
                patch.object(cli, "render", side_effect=render_fixture.render), patch.object(cli, "inspect_pdf", side_effect=workflow_fixture.inspect):
            status, output = self.run_cli()
        self.assertEqual(status, 0)
        self.assertIn("draft_ready_for_review", output)
        manifest = load_json(self.root / "data/outputs" / self.issue["issue_id"] / "revision-1/delivery-manifest.json", 512 * 1024)
        self.assertEqual(manifest["evidence_type"], "synthetic")
        self.assertEqual(manifest["languages"], ["en", "hi-Latn"])
        self.assertFalse(manifest["approved"])
        self.assertFalse(manifest["release_ready"])
        self.assertEqual(len(render_fixture.renders), 2)
        self.assertEqual(memory.snapshot()["published_history"], {})


if __name__ == "__main__":
    unittest.main()
