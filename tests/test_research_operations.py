"""Durable plans and actionable gaps, without paid collection or real AI."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory
from product_radar.research_flow import candidate_summary, run_flow
from product_radar.research_operations import (bind_prepared_action, import_trends_evidence,
    missing_market_checks, plan_research, record_query_outcome, save_source_check, write_catalogue_view)
from product_radar.storage import Journal, load_json
from product_radar.service import policy_for_fit
from test_research_planner import catalogue, outcome, NOW
from test_research_flow import FakeRunner, capture


class OperationTests(unittest.TestCase):
    def setUp(self):
        # Preserve explicit historical expectations for this legacy fixture.
        policy = policy_for_fit("fit-13-v1")
        historical_policy = patch("product_radar.service.load_policy", return_value=policy)
        historical_policy.start(); self.addCleanup(historical_policy.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")

    def test_repeat_plan_is_one_event_and_html_matches_choices(self):
        data = catalogue()
        first = plan_research(self.memory, data, as_of=NOW, slots=4)
        events = len(list((self.memory.root / "events").glob("*.json")))
        second = plan_research(self.memory, data, as_of=NOW, slots=4)
        self.assertEqual(first, second)
        self.assertEqual(events, len(list((self.memory.root / "events").glob("*.json"))))
        self.assertEqual(len(self.memory.snapshot()["batch_plans"]), 1)
        page = (self.memory.root / "next_searches.html").read_text()
        self.assertTrue(all(action["phrase"] in page for action in first["actions"]))
        self.assertFalse(self.memory.journal.path.exists())

    def test_source_failure_does_not_block_independent_discovery(self):
        source_id = save_source_check(self.memory, source="trends", route="browser", at=NOW,
                                     state="blocked", reason="Rate limited", reference="public-browser-check")
        self.assertEqual(source_id, save_source_check(self.memory, source="trends", route="browser", at=NOW,
                                     state="blocked", reason="Rate limited", reference="public-browser-check"))
        plan = plan_research(self.memory, catalogue(), as_of=NOW, slots=4)
        self.assertEqual(len(plan["actions"]), 4)
        self.assertEqual(plan["source_checks"][0]["state"], "blocked")

    def test_feedback_requires_existing_capture_and_real_date(self):
        plan = plan_research(self.memory, catalogue(), as_of=NOW, slots=1)
        value = outcome(plan["actions"][0])
        with self.assertRaisesRegex(SafetyError, "CAPTURE_REQUIRED"):
            record_query_outcome(self.memory, value)
        capture(self.memory, acquisition=value["acquisition_id"], observed=value["observed_at"])
        first = record_query_outcome(self.memory, value)
        self.assertEqual(first, record_query_outcome(self.memory, value))
        self.assertEqual(len(self.memory.snapshot()["query_outcomes"]), 1)
        value["observed_at"] = NOW
        with self.assertRaisesRegex(SafetyError, "CAPTURE_REQUIRED"):
            record_query_outcome(self.memory, value)

    def test_below_threshold_and_sufficient_coverage_do_not_queue_extra_checks(self):
        capture(self.memory)
        summary = run_flow(self.memory, FakeRunner(), as_of=NOW)
        tasks = missing_market_checks(summary, as_of=NOW)
        self.assertEqual(len(tasks), 2)
        self.assertTrue(all(t["needed_sources"] == 1 for t in tasks))
        self.assertTrue(all(len(t["choices"]) == 2 for t in tasks))
        summary["products"][0]["checklist_meets"] = 6
        summary["products"][1]["market"]["available_families"] = 2
        self.assertEqual(missing_market_checks(summary, as_of=NOW), [])

    def test_blocked_trends_check_preserves_marketplace_option(self):
        capture(self.memory)
        summary = run_flow(self.memory, FakeRunner(), as_of=NOW)
        check = {"source": "trends", "at": NOW, "state": "blocked", "reason": "HTTP 429"}
        tasks = missing_market_checks(summary, as_of=NOW, source_checks=[check])
        self.assertTrue(all(any(c["source"] == "marketplace" for c in task["choices"]) for task in tasks))
        self.assertTrue(all(next(c for c in task["choices"] if c["source"] == "trends")["route_state"] == "blocked" for task in tasks))

    def test_missing_market_tasks_prioritise_existing_positive_and_preserve_sources(self):
        capture(self.memory)
        summary = run_flow(self.memory, FakeRunner(), as_of=NOW)
        original = deepcopy(summary["products"][0])
        products = []
        for concept_id, positive in (("z-weak", 0), ("b-positive", 1), ("a-weak", 0)):
            product = deepcopy(original)
            product["id"] = concept_id
            product["market"]["positive_families"] = positive
            product["market"]["families"]["meta"].update(state="Positive" if positive else "Not-positive", positive=bool(positive))
            products.append(product)
        summary["products"] = products
        before = deepcopy(summary)
        tasks = missing_market_checks(summary, as_of=NOW)
        self.assertEqual([task["concept_id"] for task in tasks], ["b-positive", "a-weak", "z-weak"])
        self.assertEqual([task["positive_families"] for task in tasks], [1, 0, 0])
        self.assertEqual([task["positive_signal_needed"] for task in tasks], [False, True, True])
        self.assertIn("does not have to be positive", tasks[0]["reason"])
        self.assertIn("Another usable check alone does not qualify", tasks[1]["reason"])
        self.assertTrue(all("bestseller or movers-and-shakers" in next(c["question"] for c in task["choices"]
                                                                     if c["source"] == "marketplace") for task in tasks))
        self.assertEqual(summary, before)
        self.assertEqual(missing_market_checks(summary, as_of=NOW), tasks)
        plan_research(self.memory, catalogue(), as_of=NOW, slots=1, summary=summary)
        page = (self.memory.root / "next_searches.html").read_text()
        self.assertIn("Checklist-passing products needing market checks", page)
        self.assertNotIn("Promising products needing market checks", page)
        self.assertIn("Another usable check alone does not qualify", page)

    def test_catalogue_view_is_escaped_and_rough_coverage_is_not_a_product_claim(self):
        data = catalogue()
        data["families"][0]["label"] = "<img src=x onerror=alert(1)>"
        result = write_catalogue_view(self.root, data)
        page = (self.root / "catalogue.html").read_text()
        self.assertIn("&lt;img", page)
        self.assertNotIn("<img src=x", page)
        self.assertEqual(result, load_json(self.root / "catalogue_summary.json"))
        self.assertIn("not recommendations", page)

    def test_plan_action_preparation_matches_saved_family_and_reuses_task(self):
        from product_radar.models import prepared_request
        from product_radar.storage import load_json
        import test_meta_live_trial as helpers
        plan = plan_research(self.memory, catalogue(), as_of=NOW, slots=1)
        action = plan["actions"][0]
        config = deepcopy(helpers.CONFIG)
        config.update(term=action["phrase"], purpose="discovery")
        prepared = prepared_request(helpers.cli.prepare(config, "prepared-fixture")["request"])
        first = bind_prepared_action(self.memory, plan["id"], action["action_id"], prepared)
        self.assertEqual(first, bind_prepared_action(self.memory, plan["id"], action["action_id"], prepared))
        self.assertEqual(first["baseline_concepts"], [])
        self.assertFalse(first["collector_dispatched"])
        other = prepared_request(helpers.cli.prepare(config, "another-fixture")["request"])
        with self.assertRaisesRegex(SafetyError, "ALREADY_BOUND"):
            bind_prepared_action(self.memory, plan["id"], action["action_id"], other)

    def test_trends_requires_known_concept_and_keeps_failed_capture(self):
        with self.assertRaisesRegex(SafetyError, "KNOWN_CONCEPT"):
            import_trends_evidence(self.memory, concept_id="unknown", acquisition_id="csv1", csv_bytes=b"", metadata={})
        capture(self.memory)
        summary = run_flow(self.memory, FakeRunner(), as_of=NOW)
        concept_id = summary["products"][0]["id"]
        result = import_trends_evidence(self.memory, concept_id=concept_id, acquisition_id="csv1", csv_bytes=b"bad", metadata={})
        self.assertEqual(result["parsed"]["state"], "Failed")
        again = import_trends_evidence(self.memory, concept_id=concept_id, acquisition_id="csv1", csv_bytes=b"bad", metadata={})
        self.assertEqual(result, again)
        with self.assertRaisesRegex(SafetyError, "CONFLICT"):
            import_trends_evidence(self.memory, concept_id=concept_id, acquisition_id="csv1", csv_bytes=b"different", metadata={})

    def test_controlled_csv_connects_market_signal_without_reinterpreting_products(self):
        from test_trends_browser import weekly, metadata
        capture(self.memory)
        runner = FakeRunner()
        summary = run_flow(self.memory, runner, as_of=NOW)
        concept_id = summary["products"][0]["id"]
        calls = len(runner.calls)
        import_trends_evidence(self.memory, concept_id=concept_id, acquisition_id="chart1",
                               csv_bytes=weekly(), metadata=metadata())
        result = candidate_summary(self.memory, as_of=NOW)
        product = next(p for p in result["products"] if p["id"] == concept_id)
        self.assertEqual(product["market"]["available_families"], 2)
        self.assertTrue(product["market"]["families"]["trends"]["positive"])
        self.assertEqual(product["status"], "candidate_for_supplier_review")
        self.assertFalse(product["client_ready"])
        self.assertEqual(len(runner.calls), calls)

    def test_new_chart_binds_description_and_replay_does_not_rebind_after_correction(self):
        from test_trends_browser import weekly, metadata
        capture(self.memory)
        summary = run_flow(self.memory, FakeRunner(), as_of=NOW)
        concept_id = summary["products"][0]["id"]
        old = self.memory.snapshot()["concepts"][concept_id]
        first = import_trends_evidence(self.memory, concept_id=concept_id, acquisition_id="bound-chart",
                                      csv_bytes=weekly(), metadata=metadata())
        self.assertEqual(first["description_hash"], digest(old["description"]))
        changed = deepcopy(old)
        changed["description"]["function"] = "A different primary product use"
        self.memory.put("concepts", concept_id, changed, previous=old)
        replay = import_trends_evidence(self.memory, concept_id=concept_id, acquisition_id="bound-chart",
                                       csv_bytes=weekly(), metadata=metadata())
        self.assertEqual(replay, first)
        product = next(p for p in candidate_summary(self.memory, as_of=NOW)["products"] if p["id"] == concept_id)
        self.assertFalse(product["market"]["families"]["trends"]["available"])

    def test_legacy_chart_keeps_original_history_and_unchanged_product_compatibility(self):
        from product_radar.providers.trends_browser import parse_trends_csv
        from product_radar.research_memory import key
        from test_trends_browser import weekly, metadata
        capture(self.memory)
        summary = run_flow(self.memory, FakeRunner(), as_of=NOW)
        concept_id = summary["products"][0]["id"]
        evidence_id = key("trends", [concept_id, "legacy-chart"])
        legacy = {"concept_id": concept_id, "acquisition_id": "legacy-chart", "parsed": parse_trends_csv(weekly(), metadata())}
        self.memory.put("trends_evidence", evidence_id, legacy)
        replay = import_trends_evidence(self.memory, concept_id=concept_id, acquisition_id="legacy-chart",
                                       csv_bytes=weekly(), metadata=metadata())
        self.assertEqual(replay, dict(id=evidence_id, **legacy))
        product = next(p for p in candidate_summary(self.memory, as_of=NOW)["products"] if p["id"] == concept_id)
        self.assertTrue(product["market"]["families"]["trends"]["positive"])
        old = self.memory.snapshot()["concepts"][concept_id]
        changed = deepcopy(old)
        changed["description"]["function"] = "A different primary product use"
        self.memory.put("concepts", concept_id, changed, previous=old)
        product = next(p for p in candidate_summary(self.memory, as_of=NOW)["products"] if p["id"] == concept_id)
        self.assertFalse(product["market"]["families"]["trends"]["available"])
        self.assertEqual(self.memory.snapshot()["trends_evidence"][evidence_id], legacy)

    def test_new_negative_series_replaces_old_positive_but_failed_export_does_not(self):
        from test_trends_browser import weekly, metadata
        capture(self.memory)
        summary = run_flow(self.memory, FakeRunner(), as_of=NOW)
        concept_id = summary["products"][0]["id"]
        def attach(acquisition, raw, at):
            import_trends_evidence(self.memory, concept_id=concept_id, acquisition_id=acquisition,
                                  csv_bytes=raw, metadata=metadata(collected_at=at, source_reference=acquisition))
        attach("chart1", weekly(), "2026-10-06T09:00:00+00:00")
        attach("chart2", b"bad export", "2026-10-06T09:30:00+00:00")
        product = next(p for p in candidate_summary(self.memory, as_of=NOW)["products"] if p["id"] == concept_id)
        self.assertTrue(product["market"]["families"]["trends"]["positive"])
        attach("chart3", weekly([20] * 54), NOW)
        product = next(p for p in candidate_summary(self.memory, as_of=NOW)["products"] if p["id"] == concept_id)
        self.assertFalse(product["market"]["families"]["trends"]["positive"])
        later = "2026-10-06T10:30:00+00:00"
        attach("chart4", weekly([0] * 54), later)
        product = next(p for p in candidate_summary(self.memory, as_of=later)["products"] if p["id"] == concept_id)
        self.assertFalse(product["market"]["families"]["trends"]["available"])


if __name__ == "__main__":
    unittest.main()
