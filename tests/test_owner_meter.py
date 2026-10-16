"""Owner meter since a fresh start: saved local records only, never providers.

Synthetic ledgers/logs and an empty temporary store; no real research store.
"""

from copy import deepcopy
from decimal import Decimal
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from product_radar import runtime_storage
from product_radar.models import SafetyError
from product_radar.operations import operation_log_summary
from product_radar.programme import load_meter_baseline, programme_meter, save_meter_baseline
from product_radar.programme_summary import METER_BASELINE_VERSION, METER_COUNTS, owner_meter
from product_radar.research_costs import collection_cost_totals
from product_radar.research_memory import ResearchMemory
from product_radar.runtime_storage import MARKER, MARKER_VALUE, runtime_path
from product_radar.storage import Journal
from test_programme_summary import financial

START = "2026-10-13T18:00:00+00:00"
LATER = "2026-10-14T20:30:00+00:00"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import frozen_research_config  # noqa: E402
# Exact expectations use the r10 settings these tests were written against, not the growing live map.
setUpModule, tearDownModule = frozen_research_config.module_fixtures()


def baseline(pair, **changes):
    totals = collection_cost_totals(pair)
    value = {"schema_version": 1, "kind": METER_BASELINE_VERSION, "started_at": START, "reason": "Owner fresh start.",
             "carried_attempt_ids": sorted(pair[1]), "carried_known_usd": totals["known_actual_usd"],
             "carried_unsettled_hold_usd": totals["unsettled_hold_usd"], "counts": dict.fromkeys(METER_COUNTS, 0)}
    value.update(changes)
    return value


def empty_state():
    return {"concepts": {}, "assessments": {}, "jobs": {}, "batch_runs": {}, "source_checks": {},
            "observations": {}, "descriptions": {}, "associations": {}}


class OwnerMeterTests(unittest.TestCase):
    def meter(self, pair, saved, **options):
        values = dict(baseline=saved, state=empty_state(), financial_state=pair, report_pool={"eligible": []},
                      programmes=[], activity={"state": "not_recorded", "message": "No research activity has been recorded yet."},
                      run_log={"state": "known", "events": 0}, as_of=LATER)
        values.update(options)
        return owner_meter(**values)

    def test_since_start_money_excludes_carried_spending_and_flags_carried_changes(self):
        pair = financial()
        saved = baseline(pair)
        budgets, attempts = deepcopy(pair)
        attempts["new-attempt"] = dict(attempts["shared-attempt"], charge=None, read_charge=None, stage="amazon")
        attempts["shared-attempt"]["read_charge"] = "0.01"
        result = self.meter((budgets, attempts), saved)
        cost = result["cost_since_start"]
        self.assertEqual(cost["state"], "known")
        self.assertEqual(cost["provider_collections"], 1)
        self.assertEqual(cost["collections_by_source"], {"amazon": 1})
        self.assertEqual(Decimal(cost["known_usd"]), 0)
        self.assertEqual(Decimal(cost["reserved_not_billed_usd"]), Decimal("0.62"))
        self.assertEqual(cost["unknown_charge_items"], 2)
        self.assertFalse(cost["accounting_complete"])
        self.assertEqual(Decimal(cost["carried_over_known_change_usd"]), Decimal("0.01"))
        self.assertEqual(Decimal(cost["carried_over_reserved_change_usd"]), Decimal("-0.02"))
        unchanged = self.meter(pair, saved)["cost_since_start"]
        self.assertEqual((unchanged["carried_over_known_change_usd"], unchanged["carried_over_reserved_change_usd"]), ("0", "0"))
        period = next(iter(cost["account_periods"].values()))
        self.assertEqual(period["opening_usd"], "5")
        self.assertEqual(period["earmarked_for_sources_usd"], "3")
        self.assertEqual(result["elapsed"], {"seconds": 95400, "text": "1d 2h 30m"})
        self.assertEqual(result["elapsed_since_first_research_activity"]["reason"], "METER_NO_RESEARCH_ACTIVITY_YET")
        later = self.meter((budgets, attempts), saved, run_log={"state": "known", "first_event_at": "2026-10-14T20:00:00+00:00"})
        self.assertEqual(later["elapsed_since_first_research_activity"], {"seconds": 1800, "text": "0d 0h 30m"})
        self.assertEqual(result["research_since_start"]["candidates"], 0)
        self.assertEqual(result["research_now"]["report_eligible_types"], 0)
        for name in ("provider_calls", "model_calls", "research_writes", "financial_writes"):
            self.assertEqual(result[name], 0)

    def test_missing_carried_attempt_is_inconsistent_not_a_zero_bill(self):
        pair = financial()
        saved = baseline(pair, carried_attempt_ids=["failed-attempt", "lost-attempt", "shared-attempt"])
        cost = self.meter(pair, saved)["cost_since_start"]
        self.assertEqual(cost, {"state": "unknown", "reason": "METER_CARRIED_ATTEMPT_MISSING"})

    def test_meter_without_a_baseline_or_ledger_reports_unknown_not_zero(self):
        result = self.meter(None, None)
        self.assertIsNone(result["started_at"])
        self.assertEqual(result["cost_since_start"]["reason"], "METER_NOT_STARTED")
        self.assertEqual(result["elapsed"]["state"], "unknown")
        self.assertEqual(result["research_now"]["candidates"], 0)
        self.assertEqual(self.meter(None, baseline(financial()))["cost_since_start"]["reason"], "METER_LEDGER_NOT_SUPPLIED")
        invalid = self.meter(financial(), dict(baseline(financial()), counts={}))
        self.assertEqual(invalid["cost_since_start"]["reason"], "METER_BASELINE_INVALID")
        self.assertEqual(invalid["elapsed"]["reason"], "METER_BASELINE_INVALID")

    def test_counts_since_start_subtract_the_saved_baseline(self):
        saved = baseline(financial(), counts=dict(dict.fromkeys(METER_COUNTS, 0), ai_jobs=2))
        state = dict(empty_state(), jobs={"job-a": {}, "job-b": {}, "job-c": {}})
        result = self.meter(financial(), saved, state=state, report_pool=None)
        self.assertEqual(result["research_now"]["ai_jobs"], 3)
        self.assertEqual(result["research_since_start"]["ai_jobs"], 1)
        self.assertIsNone(result["research_since_start"]["report_eligible_types"])


class OperationLogSummaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="radar-meter-log-")
        self.addCleanup(temporary.cleanup)
        self.addCleanup(runtime_storage._configured.cache_clear)
        self.root = Path(temporary.name)
        self.folder = self.root / "data/operations/research-runs"
        self.folder.mkdir(parents=True)

    def write(self, name, rows, *, mtime=None):
        path = self.folder / name
        path.write_text("".join((row if isinstance(row, str) else json.dumps(row)) + "\n" for row in rows))
        if mtime is not None:
            os.utime(path, (mtime, mtime))

    def test_counts_only_records_after_the_start_with_live_counter_rules(self):
        row = {"schema_version": 1, "run_id": "run-a", "event": "stage_started"}
        self.write("run-a.jsonl", [
            dict(row, at="2026-10-13T17:59:59+00:00", event="invocation_started"),
            dict(row, at="2026-10-13T18:00:00+00:00", event="invocation_started"),
            dict(row, at="2026-10-13T18:01:00+00:00", event="saved_result", useful_result=True),
            dict(row, at="2026-10-13T18:02:00+00:00", event="source_outcome", state="failed", reason="SOURCE_BLOCKED",
                 error_reported=True),
            dict(row, at="2026-10-13T18:03:00+00:00", event="source_outcome", state="failed", reason="OLD_FAILURE",
                 outcome_reused=True, actual_attempt=False),
            dict(row, at="2026-10-13T18:04:00+00:00", event="stage_finished", state="waiting", reason="MODEL_WAIT"),
            dict(row, at="2026-10-13T18:04:30+00:00", event="programme_progress", state="usable",
                 reason="PRODUCT_IMAGE_USABLE_SAVED", useful_result=True, error_reported=True),
            "not json", dict(row, at="2026-10-13T18:05:00+00:00", event="invocation_failed", state="failed")])
        self.write("run-old.jsonl", [dict(row, run_id="run-old", at="2026-10-14T00:00:00+00:00")], mtime=0)
        result = operation_log_summary(self.root, since=START)
        self.assertEqual(result["state"], "partial")
        self.assertEqual((result["runs"], result["events"], result["useful_results"]), (1, 7, 2))
        self.assertEqual((result["failures"], result["reused_saved_failures"], result["waits"]), (2, 1, 1))
        self.assertEqual(result["top_failure_reasons"], {"SOURCE_BLOCKED": 1, "invocation_failed": 1})
        self.assertEqual(result["top_wait_reasons"], {"MODEL_WAIT": 1})
        self.assertEqual((result["invocations_started"], result["invocations_interrupted_or_failed"]), (1, 1))
        self.assertEqual(result["unreadable_lines"], 1)
        self.assertEqual(result["first_event_at"], "2026-10-13T18:00:00+00:00")
        self.assertEqual((result["access_blocks"], result["top_access_block_routes"]), (0, {}))

    def test_website_refusals_are_access_blocks_and_held_questions_are_waits(self):
        row = {"schema_version": 1, "run_id": "run-a", "event": "source_outcome", "state": "blocked"}
        self.write("run-a.jsonl", [
            dict(row, at="2026-10-13T18:01:00+00:00", route="google-maps", actual_attempt=True,
                 reason="SUPPLIER_ROUTE_CHALLENGE"),
            dict(row, at="2026-10-13T18:02:00+00:00", route="google-maps", actual_attempt=True,
                 reason="SUPPLIER_ROUTE_CHALLENGE"),
            dict(row, at="2026-10-13T18:03:00+00:00", route="indiamart", actual_attempt=True),
            # Held behind the earlier refusal: nothing was sent, so it is a wait.
            dict(row, at="2026-10-13T18:04:00+00:00", route="google-maps", actual_attempt=False,
                 reason="SOURCE_ACCESS_WAIT"),
            # A relayed progress copy is not a second refusal.
            dict(row, at="2026-10-13T18:05:00+00:00", event="programme_progress", route="google-maps",
                 actual_attempt=True)])
        result = operation_log_summary(self.root, since=START)
        self.assertEqual(result["access_blocks"], 3)
        self.assertEqual(result["top_access_block_routes"], {"google-maps": 2, "indiamart": 1})
        self.assertEqual((result["failures"], result["waits"]), (0, 1))
        self.assertEqual(result["top_wait_reasons"], {"SOURCE_ACCESS_WAIT": 1})
        self.assertEqual(result["top_failure_reasons"], {})

    def test_live_counter_uses_the_same_access_block_rules(self):
        from product_radar import operations
        with operations.operation_run(self.root), operations.operation_writer():
            operations.record_event("source_outcome", stage="acquire", state="blocked", actual_attempt=True,
                                    route="google-maps", reason="SUPPLIER_ROUTE_CHALLENGE")
            operations.record_event("source_outcome", stage="acquire", state="blocked", actual_attempt=False,
                                    route="google-maps", reason="SOURCE_ACCESS_WAIT")
            counts = operations.read_operation_status(self.root)["counts"]
        self.assertEqual((counts["access_blocks"], counts["deferred"], counts["failures"]), (1, 1, 0))

    def test_empty_folder_and_byte_bound(self):
        self.assertEqual(operation_log_summary(self.root, since=START)["events"], 0)
        self.write("run-a.jsonl", [{"run_id": "run-a", "event": "x", "at": START}] * 3)
        self.assertTrue(operation_log_summary(self.root, since=START, max_bytes=10)["truncated"])


class FreshStoreMeterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="radar-meter-store-")
        self.addCleanup(temporary.cleanup)
        self.addCleanup(runtime_storage._configured.cache_clear)
        runtime_storage._configured.cache_clear()
        self.project = Path(temporary.name) / "project"
        store = Path(temporary.name) / "store"
        (self.project / "config/local").mkdir(parents=True)
        store.mkdir()
        (store / MARKER).write_text(json.dumps(MARKER_VALUE))
        (self.project / "config/local/runtime_storage.json").write_text(json.dumps({"schema_version": 1, "runtime_root": str(store)}))
        self.journal = Journal(runtime_path(self.project, "data/benchmarks/state/apify"))
        with self.journal.locked():
            self.journal.setup_budget("synthetic-account:synthetic-period", "5.00")
        self.memory = ResearchMemory(runtime_path(self.project, "data/research/captured"), self.journal)

    def test_empty_store_meter_starts_once_and_reads_back(self):
        saved = save_meter_baseline(self.memory, root=self.project, reason="Synthetic fresh start.", as_of=START)
        self.assertEqual(saved["carried_attempt_ids"], [])
        self.assertEqual(saved["counts"], dict.fromkeys(METER_COUNTS, 0))
        self.assertEqual(load_meter_baseline(self.project), saved)
        with self.assertRaisesRegex(SafetyError, "METER_BASELINE_ALREADY_SAVED"):
            save_meter_baseline(self.memory, root=self.project, reason="Again.", as_of=LATER)
        result = programme_meter(self.memory, root=self.project, as_of=LATER)
        self.assertEqual(result["optional_input_failures"], {})
        self.assertEqual(result["cost_since_start"]["provider_collections"], 0)
        self.assertEqual(result["research_since_start"], dict.fromkeys(METER_COUNTS, 0))
        self.assertEqual(result["runs_since_start"]["events"], 0)
        self.assertEqual((result["runs_since_start"]["access_blocks"],
                          result["runs_since_start"]["top_access_block_routes"]), (0, {}))
        self.assertEqual(result["access_waits_now"], [])  # No website is refusing us.
        self.assertEqual(result["activity_now"]["state"], "not_recorded")
        self.assertEqual(result["trends_check"], "Google Trends: waiting for the owner to sign in.")  # W2 proof gate
        self.assertEqual(result["platforms"], [])  # W4: no platform waits on the owner
        self.assertIsNone(result["housekeeping"])  # no backup or store tidying problem
        self.assertTrue(result["product_map"].startswith(  # the product map's runway, from the same saved records
            "Product map: 224 headings, 224 not searched yet; the search pace is not measured yet"))
        self.assertFalse((self.project / "data").exists())

    def test_meter_lists_platforms_waiting_on_the_owner(self):
        from product_radar import dropship_platforms as platforms
        refresh = platforms.load_refresh(self.memory.root, "dropdash")
        platforms._save_refresh(self.memory.root, "dropdash",
                                dict(refresh, wait={"reason": "OWNER_SIGN_IN_REQUIRED", "at": START}))
        result = programme_meter(self.memory, root=self.project, as_of=LATER)
        self.assertEqual(result["platforms"], ["Dropdash needs you to sign in (Sign in to research sites)."])
        (self.memory.root / "dropship_platforms/dropdash/refresh.json").write_text("{corrupt")
        result = programme_meter(self.memory, root=self.project, as_of=LATER)
        self.assertIsNone(result["platforms"])
        self.assertIn("platforms", result["optional_input_failures"])

    def test_meter_command_is_read_only_and_reports_a_missing_start(self):
        import run_programme
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(run_programme.main(["--meter"], root=self.project), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["kind"], "owner-meter-v1")
        self.assertEqual(result["optional_input_failures"]["baseline"], "SAVED_FILE_UNAVAILABLE")
        self.assertFalse(runtime_path(self.project, "data/operations/meter-baseline.json").exists())
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            run_programme.main(["--meter", "--execute"], root=self.project)


if __name__ == "__main__":
    unittest.main()
