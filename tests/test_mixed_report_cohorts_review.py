"""Actual mixed legacy/new saved research through ordinary report progression."""

from pathlib import Path
import tempfile
import unittest

from product_radar.programme import _reports, prepare_programme
from product_radar.report_examples import RECOMMENDATION_CONTRACT
from product_radar.research_memory import change
from product_radar.storage import load_json
from product_radar.work_turns import bind_work_turn, close_work_turn, open_work_turn
from test_functional_source_market import functional_pool_fixture
from test_functional_report_packet import RecordingWriter
from test_programme_reports import ProgrammeReportsTests, inspect
from test_report_source_facts import source_fixture

NOW = "2026-10-06T10:00:00+00:00"


class MixedReportCohortTests(unittest.TestCase):
    def run_mixed(self, functional_count):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        memory, ids = functional_pool_fixture(root / "inputs", count=functional_count)
        legacy = source_fixture(10).state
        for assessment in legacy["assessments"].values():
            assessment["contract_version"] = "research-e-fit10-v2"
        memory.commit("synthetic-old-format-peers", [change(table, rid, row)
            for table, rows in legacy.items() for rid, row in rows.items()])
        record = prepare_programme(memory, "mixed-report-cohorts", source_run_id="mixed-original-run",
            concept_ids=sorted(ids + list(legacy["concepts"])), as_of=NOW)
        # Reuse the genuine local PDF inspector and deterministic fake renderer;
        # no writer/packet/eligibility validator is replaced.
        renderer = ProgrammeReportsTests(methodName="runTest")
        renderer.renders = []
        runner = RecordingWriter()
        context = {"at": NOW, "root": root, "runner": runner, "render_pdf": renderer.render,
            "inspect_pdf": inspect, "research_advancing": False, "retry_seconds": 3600}
        turn = open_work_turn(memory, record["programme_id"], at=NOW, report_ready=True)
        with bind_work_turn(memory, turn["id"]):
            result = _reports(memory, record, context)
        close_work_turn(memory, turn["id"], at=NOW, reason="Synthetic finite turn completed.")
        if functional_count >= 10:
            # Ten new-format products and ten old-format products need more
            # than one ordinary finite model allowance. Resume the saved
            # packets in the next genuine turn, not an enlarged test-only cap.
            context["at"] = "2026-10-06T11:01:00+00:00"
            second = open_work_turn(memory, record["programme_id"], at=context["at"], report_ready=True)
            with bind_work_turn(memory, second["id"]):
                result = _reports(memory, record, context)
        manifests = [load_json(row["manifest_path"], 512 * 1024) for row in record["drafts"].values() if row.get("complete")]
        holds = memory.snapshot()["issue_reservations"]
        return result, manifests, holds

    def test_short_functional_group_does_not_strand_complete_legacy_report(self):
        result, manifests, holds = self.run_mixed(1)
        self.assertEqual(len(manifests), 1, result)
        self.assertEqual(manifests[0]["schema_version"], 1)
        complete = [row for row in holds.values() if len(row.get("products", [])) >= 10]
        self.assertEqual(len(complete), 1)
        self.assertNotIn("recommendation_contract", complete[0])
        self.assertEqual({row["concept_id"] for row in complete[0]["products"]}, {f"c{n:03d}" for n in range(10)})

    def test_two_complete_groups_make_separate_valid_report_formats(self):
        result, manifests, holds = self.run_mixed(10)
        self.assertEqual(len(manifests), 2, result)
        self.assertEqual(sorted(row["schema_version"] for row in manifests), [1, 2])
        complete = [row for row in holds.values() if len(row.get("products", [])) >= 10]
        self.assertEqual(len(complete), 2)
        for hold in complete:
            self.assertEqual(len({row.get("recommendation_contract") for row in hold["products"]}), 1)
            self.assertEqual(hold.get("recommendation_contract"), hold["products"][0].get("recommendation_contract"))
        self.assertEqual(sum(hold.get("recommendation_contract") == RECOMMENDATION_CONTRACT for hold in complete), 1)
        self.assertEqual(len({p["concept_id"] for h in complete for p in h["products"]}), 20)


if __name__ == "__main__":
    unittest.main()
