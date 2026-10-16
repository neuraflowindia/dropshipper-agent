"""Research trace commands end to end on a synthetic data folder: plain text
and JSON for every command, the shared options before or after the command,
--out files, and the read-only promise (nothing written into the data)."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import trace_fixture as fx
from product_radar import research_trace as cli
from product_radar import trace_labels as words
from product_radar import trace_text as text

REPO = Path(__file__).resolve().parents[1]


def tree(root):
    return sorted((str(path.relative_to(root)), path.stat().st_size, path.stat().st_mtime_ns)
                  for path in root.rglob("*") if path.is_file())


class ResearchTraceCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "store"
        fx.build(self.root)
        self.before = tree(self.root)
        self.judge = patch.object(cli, "judge", return_value=fx.judged())
        self.judge.start()

    def tearDown(self):
        self.judge.stop()
        self.assertEqual(tree(self.root), self.before)  # nothing written into the data folder
        self.temp.cleanup()

    def run_cli(self, *argv):
        out = io.StringIO()
        code = cli.main(list(argv), stdout=out, now=fx.AS_OF)
        return code, out.getvalue()

    def test_every_command_as_text(self):
        for command, expected in (("funnel", "15. Report-ready types"), ("routes", "DROPSHIPPING PLATFORM CATALOGUE READS"),
                                  ("ai", "Per-job allowance percentages are not recorded"),
                                  ("summary", "NOT TRACEABLE FROM THE SAVED RECORDS TODAY")):
            code, output = self.run_cli(command, "--data-root", str(self.root))
            self.assertEqual(code, 0, command)
            self.assertIn(expected, output, command)
        code, output = self.run_cli("summary", "--data-root", str(self.root))
        self.assertIn("Types per label", output)          # key tables stay in the short summary
        self.assertNotIn("Exact reason combinations", output)
        self.assertIn("WEBSITE READS (per website, short)", output)

    def test_full_funnel_text_has_tables_and_days(self):
        code, output = self.run_cli("funnel", "--data-root", str(self.root), "--since", fx.START)
        self.assertEqual(code, 0)
        self.assertIn("Exact reason combinations", output)
        self.assertIn("PER DAY (India time", output)
        self.assertIn("10-16   10-17", output)

    def test_json_and_shared_options_before_the_command(self):
        code, output = self.run_cli("--data-root", str(self.root), "--json", "--since", fx.T4, "ai")
        self.assertEqual(code, 0)
        report = json.loads(output)
        self.assertEqual((report["kind"], report["since"], report["total"]["jobs"]), ("research-trace-ai-v1", fx.T4, 1))
        code, output = self.run_cli("summary", "--json", "--data-root", str(self.root))
        summary = json.loads(output)
        self.assertEqual(summary["since"], fx.START)
        self.assertEqual(len(summary["not_traceable"]), len(cli.NOT_TRACEABLE))
        self.assertIn("seconds", summary)

    def test_product_one_many_or_no_match(self):
        code, output = self.run_cli("product", "rack", "--data-root", str(self.root))
        self.assertEqual(code, 0)
        self.assertIn("PRODUCT TYPE JOURNEY: Folding test rack", output)
        code, output = self.run_cli("product", "test", "--data-root", str(self.root))
        self.assertEqual(code, 2)
        self.assertIn("4 product types match 'test'", output)
        code, output = self.run_cli("product", "nothing-like-this", "--data-root", str(self.root), "--json")
        self.assertEqual((code, json.loads(output)["matches"]), (2, []))
        code, output = self.run_cli("product", "nothing-like-this", "--data-root", str(self.root))
        self.assertIn("No product type matches", output)

    def test_out_file_and_refusals(self):
        target = Path(self.temp.name) / "outputs" / "funnel.json"
        code, output = self.run_cli("funnel", "--data-root", str(self.root), "--json", "--out", str(target))
        self.assertEqual((code, output), (0, ""))
        self.assertEqual(json.loads(target.read_text())["kind"], "research-trace-funnel-v1")
        with patch("sys.stderr", new=io.StringIO()) as error:
            code, _ = self.run_cli("routes", "--data-root", str(self.root), "--out", str(self.root / "x.txt"))
            self.assertEqual(code, 1)
            self.assertIn("TRACE_OUTPUT_INSIDE_RESEARCH_DATA", error.getvalue())
            code, _ = self.run_cli("routes", "--data-root", str(self.root), "--out",
                                   str(Path(self.temp.name) / "elsewhere" / ".." / "store" / "y.txt"))
            self.assertEqual(code, 1)
            code, _ = self.run_cli("routes", "--data-root", str(self.root), "--since", "yesterday")
            self.assertEqual(code, 1)
            code, _ = self.run_cli("routes", "--data-root", str(Path(self.temp.name) / "missing"))
            self.assertEqual(code, 1)
            self.assertIn("TRACE_RESEARCH_RECORDS_NOT_FOUND", error.getvalue())

    def test_project_root_and_default_data_folder(self):
        code, output = self.run_cli("routes", "--project-root", str(self.root))
        self.assertEqual(code, 0)
        self.assertIn("WEBSITE READS", output)


class RealJudgementTest(unittest.TestCase):
    def test_programme_functions_run_and_any_failure_is_named(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fx.build(root)
            out = io.StringIO()
            code = cli.main(["funnel", "--data-root", str(root), "--as-of", fx.AS_OF], stdout=out)
        self.assertEqual(code, 0)
        self.assertIn("RESEARCH FUNNEL", out.getvalue())

    def test_programme_functions_never_write_into_the_data_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fx.build(root)
            before = tree(root)
            for argv in (["funnel"], ["routes"], ["ai"], ["product", "rack"], ["summary", "--json"]):
                code = cli.main([*argv, "--data-root", str(root), "--as-of", fx.AS_OF], stdout=io.StringIO())
                self.assertEqual(code, 0, argv)
            self.assertEqual(tree(root), before)

    def test_script_entry_point(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fx.build(root)
            result = subprocess.run([sys.executable, str(REPO / "scripts" / "research_trace.py"), "routes", "--json",
                                     "--data-root", str(root)], capture_output=True, text=True, timeout=120,
                                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["kind"], "research-trace-routes-v1")


class PlainWordsTest(unittest.TestCase):
    def test_labels_always_show_the_code(self):
        self.assertEqual(words.route("meta-ads"), "Meta Ad Library (Facebook/Instagram ads) [meta-ads]")
        self.assertEqual(words.route("new-site"), "Other website [new-site]")
        self.assertEqual(words.route(None), "Unknown website")
        self.assertEqual(words.state(None), "No status saved")
        self.assertEqual(words.state("odd"), "Other status [odd]")
        self.assertEqual(words.reason(None), "No reason saved")
        self.assertEqual(words.reason("acquired"), "Answered with results [acquired]")
        self.assertEqual(words.reason("NEW_CODE"), "Other reason [NEW_CODE]")
        self.assertEqual(words.reason("Plain sentence from the programme."), "Plain sentence from the programme.")
        self.assertEqual(len(words.reason("x " * 300)), 200)
        self.assertEqual(words.label(None), "Not labelled yet")
        self.assertEqual(words.label("odd"), "Other label [odd]")
        self.assertEqual(words.ai_step(None, "B"), "Product type identity [B]")
        self.assertEqual(words.ai_step("research-new-v9", None), "Other AI step [research-new-v9]")
        self.assertEqual(words.ai_step(None, None), "Other AI step [unknown]")
        self.assertEqual(words.description_kind("odd"), "Other answer [odd]")
        self.assertEqual(words.source("amazon"), "Amazon [amazon]")
        self.assertEqual(words.source("tradeindia"), "TradeIndia [tradeindia]")
        self.assertEqual(words.source("elsewhere"), "elsewhere")

    def test_text_helpers(self):
        self.assertEqual([text.number(value) for value in (None, True, 1234, 2.25, "x")], ["-", "True", "1,234", "2.2", "x"])
        self.assertEqual(text.when(None), "-")
        self.assertEqual(text.when("not a time"), "not a time")
        self.assertEqual(text.when("2026-10-16T12:00:00+05:30"), "2026-10-16 06:30 UTC")
        self.assertEqual([text.duration(value) for value in (None, 30, 600, 7200)],
                         ["-", "30 s", "600 s (10 min)", "7,200 s (2.0 h)"])
        self.assertEqual(text.header("T", {})[:2], ["T", "="])
        self.assertIn("Note: n", "\n".join(text.rows({"title": "t", "rows": [], "note": "n"})))


if __name__ == "__main__":
    unittest.main()
