"""Python/website fact-contract parity, using disposable fictional research.

No model, account, hosted request or working research state is accessed. The
real CSV parser, fact builder and saved writing workflow supply the packet.
"""

from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_fit_pipeline import WritingFixture
from test_report_selection import OBSERVED
from test_report_source_facts import source_fixture, select
from test_trends_browser import metadata, weekly
from product_radar.models import canonical, digest
from product_radar.providers.trends_browser import parse_trends_csv
from product_radar.report_narration import generate_report_narratives
from product_radar.research_memory import ResearchMemory, change
from product_radar.storage import Journal


@unittest.skipUnless(shutil.which("node"), "Website runtime is not installed")
class PortalSourceContractTests(unittest.TestCase):
    def test_real_fact_builder_fractional_chart_and_complete_writing_match_website_hash_and_validation(self):
        initial = source_fixture()
        parsed = parse_trends_csv(
            weekly([20] * 45 + [20, 20, 20, 21] + [30, 30, 30, 31] + [100]),
            metadata(collected_at=OBSERVED),
        )
        initial.state["trends_evidence"]["fractional-chart"] = {
            "concept_id": "c000", "description_hash": digest(initial.state["concepts"]["c000"]["description"]),
            "parsed": parsed,
        }
        with tempfile.TemporaryDirectory(prefix="neuraflow-source-parity-") as directory:
            memory = ResearchMemory(Path(directory) / "research", Journal(Path(directory) / "finance"), evidence_type="synthetic")
            memory.commit("synthetic-source-parity", [change(table, record_id, row)
                for table, records in initial.state.items() for record_id, row in records.items()])
            _, issue, facts = select(memory)
            result = generate_report_narratives(memory, issue, facts, WritingFixture())
            self.assertEqual(result["failures"], {})
            self.assertEqual(result["complete_languages"], ["en", "hi-Latn"])
            facts = result["dataset"]
            manifest = {"fit_policy_id": facts["fit_policy_id"], "criterion_ids": facts["criterion_ids"],
                "kpi_total": 10, "workbook_contract": "fit-checklist-v1", "languages": ["en", "hi-Latn"],
                "opportunity_policy_id": facts["opportunity_policy_id"]}
            script = (
                "import {readFileSync} from 'node:fs'; import {createHash} from 'node:crypto'; "
                "import {canonical, validateFitPacket} from " + json.dumps((ROOT / "portal/release-admin.mjs").as_uri()) + "; "
                "const input=JSON.parse(readFileSync(0,'utf8')); validateFitPacket(input.facts,input.manifest); "
                "const hash=createHash('sha256').update(canonical(input.facts)).digest('hex'); "
                "if(hash!==input.expected_hash) throw new Error('FACT_HASH_MISMATCH'); "
                "process.stdout.write(JSON.stringify({hash, products:input.facts.products.length," 
                "routes:input.facts.products[0].source_route_checks.length}));"
            )
            completed = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script],
                input=canonical({"facts": facts, "manifest": manifest, "expected_hash": digest(facts)}),
                capture_output=True, timeout=20, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
            proof = json.loads(completed.stdout)
            self.assertEqual(proof, {"hash": digest(facts), "products": 10, "routes": 13})
            chart = next(row for row in facts["products"][0]["source_route_checks"] if row["route"] == "google-trends")
            means = {metric["name"]: metric["value"] for metric in chart["measurements"]}
            self.assertEqual(means["previous_mean_bounds"], ["20.25", "20.25"])
            self.assertEqual(means["recent_mean_bounds"], ["30.25", "30.25"])


if __name__ == "__main__":
    unittest.main()
