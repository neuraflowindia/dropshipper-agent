"""Offline regressions for local discovery waits, never provider requests."""

from copy import deepcopy
from datetime import timedelta
import unittest
from unittest.mock import patch

import product_radar.programme as programme
from product_radar.catalogue_discovery import WAVE_VERSION
from product_radar.models import SafetyError, digest, timestamp
from product_radar.service import load_policy
from test_catalogue_expansion import fixture as catalogue_fixture
import test_programme as fixtures
from test_programme_summary import account_status, financial


class ProgrammeDiscoveryFailureReviewTests(unittest.TestCase):
    def fixture(self):
        fixture = fixtures.ProgrammeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.prepare()
        return fixture

    def discovery(self, failure, calls):
        def step(_memory, _record, _context):
            calls.append("discovery")
            raise failure
        step.discovery_wave_contract = WAVE_VERSION
        step.discovery_catalogue = catalogue_fixture()
        step.discovery_wave_options = {}
        return step

    def steps(self, calls):
        return {stage: (lambda _memory, _record, _context, name=stage:
                       calls.append(name) or {"state": "nothing_new"})
                for stage in programme.STAGES}

    def money(self, fixture):
        with fixture.memory.journal.locked():
            return fixture.memory.journal.state()

    def readiness_failure(self, failure, reason):
        fixture = self.fixture()
        record = programme.load_programme(fixture.memory, "whole-programme")
        state = fixture.memory.snapshot()
        money = self.money(fixture)
        calls, inputs = [], []
        discovery = self.discovery(failure, calls)
        def watched_digest(value):
            inputs.append(deepcopy(value))
            return digest(value)
        with patch("product_radar.catalogue_discovery.discovery_wave_readiness", side_effect=failure), \
             patch("product_radar.programme.digest", side_effect=watched_digest):
            first = programme.programme_turn_readiness(fixture.memory, record, root=fixture.root,
                handlers={}, discovery_step=discovery, steps=self.steps(calls), as_of=fixture.now, state=state)
            second = programme.programme_turn_readiness(fixture.memory, record, root=fixture.root,
                handlers={}, discovery_step=discovery, steps=self.steps(calls), as_of=fixture.now, state=state)
        expected_signal = [["discovery_wait", reason, type(failure).__name__,
                            digest(discovery.discovery_catalogue), digest(state["catalogue_additions"])]]
        self.assertTrue(any(isinstance(value, list) and expected_signal in value for value in inputs))
        self.assertEqual(first, second)
        self.assertTrue(first["ready"])
        self.assertFalse(first["ready_lanes"]["exploration"])
        self.assertEqual(calls, [])
        self.assertEqual(fixture.memory.snapshot(), state)
        self.assertEqual(self.money(fixture), money)
        self.assertNotIn("private fixture detail", str(inputs))

    def test_catalogue_conflict_is_a_named_pure_local_readiness_wait(self):
        self.readiness_failure(SafetyError("CATALOGUE_ADDITION_FAMILY_CONFLICT"),
                               "CATALOGUE_ADDITION_FAMILY_CONFLICT")

    def test_file_failure_is_a_named_pure_local_readiness_wait(self):
        self.readiness_failure(OSError("private fixture detail"), "PROGRAMME_DISCOVERY_LOCAL_FILE_WAIT")

    def test_value_failure_is_a_named_pure_local_readiness_wait(self):
        self.readiness_failure(ValueError("private fixture detail"), "PROGRAMME_DISCOVERY_READINESS_FAILED")

    def test_normal_cooperative_producer_records_failure_and_runs_every_independent_stage(self):
        for failure, reason in ((SafetyError("CATALOGUE_ADDITION_FAMILY_CONFLICT"),
                                 "CATALOGUE_ADDITION_FAMILY_CONFLICT"),
                                (OSError("private fixture detail"), "PROGRAMME_DISCOVERY_FAILED"),
                                (ValueError("private fixture detail"), "PROGRAMME_DISCOVERY_FAILED")):
            with self.subTest(error=type(failure).__name__):
                fixture = self.fixture()
                before = fixture.memory.snapshot()
                money = self.money(fixture)
                calls = []
                discovery = self.discovery(failure, calls)
                with patch("product_radar.catalogue_discovery.discovery_wave_readiness", side_effect=failure):
                    result = programme.advance_ready_programme_turn(fixture.memory, root=fixture.root,
                        programme_id="whole-programme", handlers={}, steps=self.steps(calls),
                        discovery_step=discovery, as_of=fixture.now)
                saved = programme.load_programme(fixture.memory, "whole-programme")
                self.assertTrue(result["round_completed"])
                self.assertEqual(calls, ["discovery", *programme.STAGES])
                self.assertEqual(saved["last_results"]["discovery"], {
                    "state": "waiting", "reason": reason, "error_type": type(failure).__name__})
                self.assertEqual(saved["deferrals"]["lane:discovery"]["reason"], reason)
                self.assertEqual(saved["deferrals"]["lane:discovery"]["next_retry_at"],
                    (timestamp(fixture.now) + timedelta(seconds=300)).isoformat())
                for stage in ("fit", "photos", "reports"):
                    self.assertEqual(saved["last_results"][stage], {"state": "nothing_new"})
                self.assertEqual(result["work_turn"]["counters"]["source_callbacks"], 0)
                self.assertEqual(result["work_turn"]["counters"]["model_executions"], 0)
                self.assertFalse(result["client_publication_attempted"])
                self.assertEqual(fixture.memory.snapshot()["published_history"], before["published_history"])
                self.assertEqual(self.money(fixture), money)
                self.assertNotIn("private fixture detail", str(saved))

    def test_unchanged_discovery_failure_settles_then_wakes_at_its_saved_retry(self):
        fixture = self.fixture()
        calls = []
        failure = SafetyError("CATALOGUE_ADDITION_FAMILY_CONFLICT")
        discovery = self.discovery(failure, calls)
        options = {"root": fixture.root, "programme_id": "whole-programme", "handlers": {},
                   "steps": self.steps(calls), "discovery_step": discovery}
        with patch("product_radar.catalogue_discovery.discovery_wave_readiness", side_effect=failure):
            first = programme.advance_ready_programme_turn(fixture.memory, as_of=fixture.now, **options)
            old_calls = list(calls)
            waiting = programme.advance_ready_programme_turn(fixture.memory, as_of=fixture.now, **options)
            self.assertTrue(first["round_completed"])
            self.assertEqual(waiting["state"], "all_waiting")
            self.assertFalse(waiting["round_completed"])
            self.assertEqual(calls, old_calls)
            later = (timestamp(fixture.now) + timedelta(seconds=301)).isoformat()
            retry = programme.advance_ready_programme_turn(fixture.memory, as_of=later, **options)
        self.assertTrue(retry["round_completed"])
        self.assertEqual(calls.count("discovery"), 2)
        for stage in ("fit", "photos", "reports"):
            self.assertEqual(calls.count(stage), 2)

    def test_readiness_research_integrity_failure_remains_fatal_before_any_stage(self):
        fixture = self.fixture()
        before = fixture.memory.snapshot()
        money = self.money(fixture)
        calls = []
        failure = SafetyError("RESEARCH_EVENTS_INVALID")
        with patch("product_radar.catalogue_discovery.discovery_wave_readiness", side_effect=failure):
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
                programme.advance_ready_programme_turn(fixture.memory, root=fixture.root,
                    programme_id="whole-programme", handlers={}, steps=self.steps(calls),
                    discovery_step=self.discovery(failure, calls), as_of=fixture.now)
        self.assertEqual(calls, [])
        # Normal selection may save its inventory boundary before advisory
        # readiness. That control checkpoint is not a provider/model launch
        # or a new work turn; all research and publication tables stay intact.
        after = fixture.memory.snapshot()
        for table in before:
            if table != "batch_runs":
                self.assertEqual(after[table], before[table], table)
        self.assertFalse(any(row.get("kind") == "work-turn-v1" for row in after["batch_runs"].values()))
        self.assertEqual(self.money(fixture), money)

    def test_summary_catalogue_conflicts_stay_named_unknown_without_hiding_other_sections(self):
        fixture = self.fixture()
        record = programme.load_programme(fixture.memory, "whole-programme")
        state = fixture.memory.snapshot()
        money = self.money(fixture)
        for reason in ("CATALOGUE_ADDITION_SOURCE_CONFLICT", "CATALOGUE_ADDITION_FAMILY_CONFLICT",
                       "CATALOGUE_ADDITION_MATCH_MISSING"):
            with self.subTest(reason=reason):
                with patch("product_radar.catalogue.load_catalogue", return_value=catalogue_fixture()), \
                     patch("product_radar.catalogue_expansion.effective_catalogue", side_effect=SafetyError(reason)), \
                     patch.object(fixture.memory, "snapshot", side_effect=AssertionError("Summary rereplayed research")):
                    value = programme.programme_owner_summary(fixture.memory, record, root=fixture.root,
                        state=state, as_of=fixture.now, financial_state=financial(), account_status=account_status(),
                        report_pool={"eligible": [], "decisions": []}, allocations={"issues": []}, policy=load_policy(),
                        effort_rows={}, cost_baseline={})
                self.assertEqual(value["optional_input_failures"], {"catalogue": reason})
                self.assertEqual(value["catalogue"]["state"], "unknown")
                self.assertEqual(value["money"]["state"], "known")
                self.assertEqual(value["research"]["original_pool"]["products"], 2)
                self.assertFalse(value["client_publication_attempted"])
        self.assertEqual(fixture.memory.snapshot(), state)
        self.assertEqual(self.money(fixture), money)

    def test_optional_summary_never_swallows_shared_research_or_money_integrity(self):
        for reason in ("RESEARCH_EVENTS_INVALID", "PROGRAMME_CONTROL_INVALID", "JOURNAL_CORRUPT_REVIEW_REQUIRED",
                       "JOURNAL_STATE_INVALID", "EVENT_ID_CONFLICT"):
            with self.subTest(reason=reason):
                failures = {}
                def broken():
                    raise SafetyError(reason)
                with self.assertRaisesRegex(SafetyError, reason):
                    programme._summary_input(broken, failures, "catalogue")
                self.assertEqual(failures, {})


if __name__ == "__main__":
    unittest.main()
