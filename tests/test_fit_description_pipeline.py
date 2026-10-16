"""Description estimates through actual joins, report files and website checks.

All products and observations are fictional. No working research, model,
provider, website or account is read or modified.
"""

from copy import deepcopy
from io import BytesIO
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import unittest

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from test_fit_pipeline import NOW, WritingFixture
from test_report_source_facts import source_fixture, select
from product_radar.fit_description import DESCRIPTION_CONTRACT, DESCRIPTION_INFERENCE_KIND
from product_radar.fit_policy import FIT_POLICY_ID
from product_radar.report_source_facts import CLIENT_ROUTES
from product_radar.fit_review import PRACTICAL_CONTRACT, prepare_fit_review, run_fit_reviews
from product_radar.live_outputs import validate_dataset, _workbook_bytes, render_html
from product_radar.models import SafetyError, canonical, digest
from product_radar.report_narration import make_narrative_input, generate_report_narratives
from product_radar.report_selection import build_report_pool
from product_radar.research_flow import current_assessment_fit
from product_radar.research_memory import ResearchMemory, change
from product_radar.storage import Journal
from test_fit_pipeline import older_format_scores


def description_fixture(count=10):
    memory = source_fixture(count)
    for concept_id, record in memory.state["assessments"].items():
        description = memory.state["concepts"][concept_id]["description"]
        record.update(contract_version=DESCRIPTION_CONTRACT,
                      fit_description_contract=DESCRIPTION_INFERENCE_KIND)
        for row in record["result"]["criteria"]:
            row["description_inference"] = None
            if row["state"] == "Meets":
                row.update(basis="inferred", references=[],
                    reason="Estimated: the described mechanism reduces ordinary handling steps.",
                    description_inference={"kind": DESCRIPTION_INFERENCE_KIND,
                        "description_hash": digest(description), "fields": ["function", "mechanism"]})
    return memory


@older_format_scores
class DescriptionPipelineTests(unittest.TestCase):
    def test_current_assessment_counts_estimates_without_creating_a_market_observation(self):
        memory = description_fixture(1); before = deepcopy(memory.state)
        fit = current_assessment_fit(memory.state, "c000", expected_policy_id=FIT_POLICY_ID)
        self.assertEqual(fit["kpi_matches"], 6)
        self.assertEqual(fit["fit_tier"], "good")
        pool = build_report_pool(memory, NOW, fit_policy_id=FIT_POLICY_ID)
        self.assertEqual(len(pool["eligible"]), 1)
        row = pool["eligible"][0]
        # The relevant ad earns want points; the Amazon listing gives none (crowding only).
        from product_radar.opportunity_score import market_evidence
        self.assertEqual(market_evidence(row), {"meta": ["meta000"]})
        self.assertEqual(row["opportunity_label"], "sweet_spot")
        self.assertEqual(memory.state, before)

    def test_declared_contract_and_exact_description_are_required(self):
        for mutate in (
            lambda record: record.pop("fit_description_contract"),
            lambda record: record.update(fit_description_contract="invented"),
            lambda record: record.update(contract_version="research-e-fit10-v2"),
            lambda record: record["result"]["criteria"][0]["description_inference"].update(description_hash="f" * 64),
        ):
            memory = description_fixture(1); mutate(memory.state["assessments"]["c000"])
            self.assertIsNone(current_assessment_fit(memory.state, "c000", expected_policy_id=FIT_POLICY_ID))

    def test_legacy_accepted_judgments_keep_their_original_meaning(self):
        memory = source_fixture(1)
        fit = current_assessment_fit(memory.state, "c000", expected_policy_id=FIT_POLICY_ID)
        self.assertEqual(fit["kpi_matches"], 6)
        pool = build_report_pool(memory, NOW, fit_policy_id=FIT_POLICY_ID)
        self.assertNotIn("fit_description_contract", pool["facts_by_concept"]["c000"])
        self.assertNotIn("fit_description_contract", pool["eligible"][0])

    def test_frozen_selection_and_writing_facts_preserve_the_separate_estimate_basis(self):
        memory = description_fixture()
        pool, issue, dataset = select(memory)
        for frozen, product in zip(issue["products"], dataset["products"]):
            self.assertEqual(frozen["fit_description_contract"], DESCRIPTION_INFERENCE_KIND)
            self.assertEqual(product["fit_description_contract"], DESCRIPTION_INFERENCE_KIND)
            self.assertEqual(len(product["source_route_checks"]), len(CLIENT_ROUTES))  # Retired Google Search has no client row.
            self.assertEqual(len(product["writing_facts"]), 6)
            self.assertTrue(all(fact["references"] == [] and fact["description_inference"]["description_hash"] ==
                digest(product["description"]) for fact in product["writing_facts"]))
            self.assertFalse(any(ref["id"].startswith("description-") for ref in product["source_refs"]))
        validate_dataset(issue, dataset)
        payload = make_narrative_input(dataset, "en")
        product = next(item for item in payload["items"] if item["kind"] == "product")
        benefit = next(fact for fact in product["facts"] if fact["id"] == "benefit-0")
        self.assertEqual(benefit["basis"], "inferred")
        self.assertTrue(benefit["text"].startswith("Estimated:"))
        self.assertEqual(benefit["references"], [])
        self.assertNotIn("description_inference", benefit)
        self.assertEqual(pool["eligible"][0]["kpi_matches"], 6)

    def test_dataset_rejects_changed_marker_or_inference_fields(self):
        _, issue, dataset = select(description_fixture())
        for mutate in (
            lambda product: product.pop("fit_description_contract"),
            lambda product: product.update(fit_description_contract="invented"),
            lambda product: product["criteria"][0]["description_inference"].update(fields=["brand"]),
            lambda product: product["criteria"][0]["description_inference"].update(description_hash="f" * 64),
            lambda product: product["criteria"][0].pop("description_inference"),
        ):
            changed = deepcopy(dataset); mutate(changed["products"][0])
            with self.assertRaises(SafetyError):
                validate_dataset(issue, changed)

    def test_narration_cannot_promote_an_unaccepted_description_benefit(self):
        _, _, dataset = select(description_fixture())
        for mutate in (
            lambda product: product["writing_facts"][0].update(text="Estimated: unrelated invented benefit."),
            lambda product: product["writing_facts"][0]["description_inference"].update(fields=["brand"]),
            lambda product: product.pop("fit_description_contract"),
        ):
            changed = deepcopy(dataset); mutate(changed["products"][0])
            with self.assertRaises(SafetyError):
                make_narrative_input(changed, "en")

    def test_marker_removal_cannot_admit_an_unbound_empty_reference_benefit(self):
        _, issue, dataset = select(description_fixture())
        for mutation in ("remove", "null"):
            changed = deepcopy(dataset)
            benefit = changed["products"][0]["writing_facts"][0]
            if mutation == "remove":
                benefit.pop("description_inference")
            else:
                benefit["description_inference"] = None
            benefit["text"] = "Estimated: this invented benefit is absent from the accepted checklist."
            validate_dataset(issue, changed)
            with self.subTest(mutation=mutation), self.assertRaisesRegex(SafetyError, "REPORT_WRITING_FACT_INVALID"):
                make_narrative_input(changed, "en")

    def test_unmarked_historical_empty_reference_writing_fact_is_not_reinterpreted(self):
        _, issue, dataset = select(source_fixture())
        product = dataset["products"][0]
        self.assertNotIn("fit_description_contract", product)
        product["writing_facts"][0]["references"] = []
        validate_dataset(issue, dataset)
        payload = make_narrative_input(dataset, "en")
        item = next(row for row in payload["items"] if row["kind"] == "product")
        self.assertTrue(any(row["id"] == "benefit-0" and row["references"] == [] for row in item["facts"]))

    @unittest.skipUnless(shutil.which("node"), "Website runtime is not installed")
    def test_v2_to_v3_partial_review_passes_real_report_and_website_contract(self):
        from test_fit_description import DescriptionRunner
        with tempfile.TemporaryDirectory(prefix="neuraflow-partial-description-pipeline-") as directory:
            memory = ResearchMemory(Path(directory) / "research", Journal(Path(directory) / "finance"), evidence_type="synthetic")
            initial = source_fixture()
            for record in initial.state["assessments"].values():
                record["contract_version"] = PRACTICAL_CONTRACT
            original = deepcopy(initial.state["assessments"]["c000"])
            memory.commit("synthetic-partial-description-pipeline", [change(table, record_id, row)
                for table, records in initial.state.items() for record_id, row in records.items()])
            base = prepare_fit_review(memory, "c000", requested_criterion_ids=[7],
                reason="synthetic-one-question-description-review", as_of=NOW, contract_version=DESCRIPTION_CONTRACT)
            self.assertEqual(base["binding"]["parent_assessment"], original)
            self.assertEqual(run_fit_reviews(memory, DescriptionRunner(), [base["reference"]])[0]["status"], "accepted")
            self.assertIsNotNone(current_assessment_fit(memory.snapshot(), "c000", expected_policy_id=FIT_POLICY_ID))
            _, issue, facts = select(memory)
            writer = WritingFixture()
            result = generate_report_narratives(memory, issue, facts, writer)
            self.assertEqual(result["failures"], {})
            facts = result["dataset"]
            validate_dataset(issue, facts)
            product = next(row for row in facts["products"] if row["concept_id"] == "c000")
            self.assertEqual(product["fit_description_contract"], DESCRIPTION_INFERENCE_KIND)
            self.assertTrue(all("description_inference" in row for row in product["criteria"]))
            self.assertTrue(all(row["description_inference"] is None for row in product["criteria"] if row["id"] != 7))
            manifest = {"fit_policy_id": facts["fit_policy_id"], "criterion_ids": facts["criterion_ids"],
                "kpi_total": 10, "workbook_contract": "fit-checklist-v1", "languages": ["en", "hi-Latn"],
                "opportunity_policy_id": facts["opportunity_policy_id"]}
            script = (
                "import{readFileSync}from'node:fs';import{createHash}from'node:crypto';"
                "import{canonical,validateFitPacket}from" + json.dumps((ROOT / "portal/release-admin.mjs").as_uri()) + ";"
                "const p=JSON.parse(readFileSync(0,'utf8'));validateFitPacket(p.facts,p.manifest);"
                "if(createHash('sha256').update(canonical(p.facts)).digest('hex')!==p.hash)throw Error('FACT_HASH_MISMATCH');")
            checked = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script],
                input=canonical({"facts": facts, "manifest": manifest, "hash": digest(facts)}),
                capture_output=True, timeout=20, check=False)
            self.assertEqual(checked.returncode, 0, checked.stderr.decode(errors="replace"))

    def written(self, directory):
        memory = ResearchMemory(Path(directory) / "research", Journal(Path(directory) / "finance"), evidence_type="synthetic")
        initial = description_fixture()
        memory.commit("synthetic-description-pipeline", [change(table, record_id, row)
            for table, records in initial.state.items() for record_id, row in records.items()])
        _, issue, facts = select(memory)
        writer = WritingFixture()
        result = generate_report_narratives(memory, issue, facts, writer)
        self.assertEqual(result["failures"], {})
        self.assertEqual(result["complete_languages"], ["en", "hi-Latn"])
        return memory, issue, result["dataset"], writer

    def test_bilingual_report_and_workbook_label_the_same_estimates_and_replay_without_model(self):
        with tempfile.TemporaryDirectory(prefix="neuraflow-description-pipeline-") as directory:
            memory, issue, facts, writer = self.written(directory)
            calls = writer.calls
            again = generate_report_narratives(memory, issue, facts, writer)
            self.assertEqual(writer.calls, calls)
            self.assertEqual(again["dataset"], facts)
            for language in ("en", "hi-Latn"):
                html = render_html(issue, facts, language=language)
                tag = '<span class="est">' + ("estimated" if language == "en" else "andaaza") + '</span>'
                estimated = sum(row["basis"] == "inferred" for product in facts["products"] for row in product["criteria"])
                self.assertGreater(estimated, 0)
                self.assertEqual(html.count(tag), estimated + 1)  # plus the "how to read" page's legend
            workbook = load_workbook(BytesIO(_workbook_bytes(issue, facts)), data_only=False)
            rows = list(workbook["Checklist"].values)
            self.assertTrue(any(any(isinstance(value, str) and value.startswith("Estimated:") for value in row) for row in rows[1:]))
            self.assertEqual(len(facts["products"]), 10)

    @unittest.skipUnless(shutil.which("node"), "Website runtime is not installed")
    def test_real_python_packet_matches_website_contract_and_canonical_hash(self):
        with tempfile.TemporaryDirectory(prefix="neuraflow-description-parity-") as directory:
            _, _, facts, _ = self.written(directory)
            manifest = {"fit_policy_id": facts["fit_policy_id"], "criterion_ids": facts["criterion_ids"],
                "kpi_total": 10, "workbook_contract": "fit-checklist-v1", "languages": ["en", "hi-Latn"],
                "opportunity_policy_id": facts["opportunity_policy_id"]}
            script = (
                "import{readFileSync}from'node:fs';import{createHash}from'node:crypto';"
                "import{canonical,validateFitPacket}from" + json.dumps((ROOT / "portal/release-admin.mjs").as_uri()) + ";"
                "const p=JSON.parse(readFileSync(0,'utf8'));validateFitPacket(p.facts,p.manifest);"
                "const h=createHash('sha256').update(canonical(p.facts)).digest('hex');"
                "if(h!==p.hash)throw Error('FACT_HASH_MISMATCH');"
                "process.stdout.write(JSON.stringify({hash:h,products:p.facts.products.length}));")
            result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script],
                input=canonical({"facts": facts, "manifest": manifest, "hash": digest(facts)}),
                capture_output=True, timeout=20, check=False)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            self.assertEqual(json.loads(result.stdout), {"hash": digest(facts), "products": 10})


if __name__ == "__main__":
    unittest.main()
