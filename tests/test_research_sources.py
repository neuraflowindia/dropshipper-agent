"""Offline contracts and fictional transport tests. Never launch a collector."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar.models import SafetyError, prepared_request, request_stage
from product_radar.research_sources import SOURCES, AMAZON_CASES, prepare_source, map_research_rows, checked_source_pricing
from product_radar.storage import atomic_json, load_json
from product_radar.http_io import validate_spec
from product_radar.benchmark import MetaTrial, run_data
import test_meta_live_trial as meta_helpers
from test_meta_live_trial import Provider, TOKEN, cli, Response


def request(source="amazon", case="kitchen-bestsellers", attempt="source-test"):
    return prepared_request(prepare_source(source, case, attempt, datetime.now(timezone.utc).isoformat())["request"])


def amazon_row():
    return {"asin": "B012345678", "title": "Fictional sink organiser", "position": 1,
            "link": "https://www.amazon.in/dp/B012345678", "categoryUrl": AMAZON_CASES["kitchen-bestsellers"],
            "category": "Best Sellers in Home & Kitchen", "price": "₹249.00", "currency": "INR", "status": 200}


def trends_row(term="sink organiser", series=None):
    end = int(datetime.now(timezone.utc).timestamp()) - 86400
    return {"keyword": term, "geo": "IN", "timeRange": "today 12-m", "category": "All categories",
            "googleProperty": "Web Search", "trendsUrl": "https://trends.google.com/trends/explore?" + urlencode({"q": term, "geo": "IN", "date": "today 12-m"}),
            "interestOverTime": series if series is not None else [{"date": str(end - 604800), "value": 0}, {"date": str(end), "value": 10}]}


class MappingTests(unittest.TestCase):
    def test_four_amazon_requests_and_trends(self):
        for case in AMAZON_CASES:
            r = request(case=case)
            self.assertEqual(request_stage(r), "amazon")
            self.assertEqual(prepared_request(r.to_dict()), r)
        self.assertEqual(request_stage(request("trends", None)), "trends")

    def test_mutated_contracts_refused(self):
        r = request()
        for changes in ({"actor": "unreviewed~actor"}, {"build": "latest"}, {"reserved_usd": "1"},
                        {"public_metadata_hash": "0" * 64}, {"purpose": "bestseller"},
                        {"input": dict(r.input, nextPage=True)}, {"input": dict(r.input, subcategoryLevel=False)},
                        {"input": dict(r.input, urls=["https://www.amazon.com/gp/bestsellers/kitchen"])}):
            with self.subTest(changes=changes), self.assertRaises(SafetyError): replace(r, **changes)
        t = request("trends", None)
        for field, value in (("geo", ""), ("mode", "trending"), ("timeRange", "today 3-m"), ("exploreConcurrency", True), ("runTag", "not-supported")):
            with self.subTest(field=field), self.assertRaises(SafetyError): replace(t, input=dict(t.input, **{field: value}))

    def test_metadata_hash_pricing_and_permission_drift(self):
        for source, case in (("amazon", "kitchen-bestsellers"), ("trends", None)):
            saved = load_json(ROOT / "tests/fixtures/benchmarks" / (source + "_metadata.json"), 524288)
            r = request(source, case)
            self.assertEqual(checked_source_pricing(saved["actor"], saved["build"], r)["plan"], "Free")
            for part, field, value in (("actor", "actorPermissionLevel", "FULL_PERMISSIONS"),
                                       ("actor", "isPPEPlatformUsagePaidByUser", True), ("build", "id", "different")):
                copy = deepcopy(saved); copy[part]["data"][field] = value
                with self.assertRaisesRegex(SafetyError, "METADATA_CHANGED"):
                    checked_source_pricing(copy["actor"], copy["build"], r)
            copy = deepcopy(saved); copy["build"]["data"]["actorDefinition"]["input"]["properties"]["newField"] = {"type": "string"}
            with self.assertRaisesRegex(SafetyError, "METADATA_CHANGED"):
                checked_source_pricing(copy["actor"], copy["build"], r)
            copy = deepcopy(saved); copy["actor"]["data"]["pricingInfos"][-1]["pricingPerEvent"]["actorChargeEvents"] = {}
            with self.assertRaisesRegex(SafetyError, "METADATA_CHANGED"):
                checked_source_pricing(copy["actor"], copy["build"], r)

    def test_endpoint_allowlist_is_still_narrow(self):
        for spec in SOURCES.values():
            validate_spec({"method": "POST", "path": "/v2/acts/" + spec["actor"] + "/runs", "query": {}, "body": {}, "byte_limit": 65536, "token": TOKEN})
        with self.assertRaises(SafetyError):
            validate_spec({"method": "POST", "path": "/v2/acts/other~actor/runs", "query": {}, "body": {}, "byte_limit": 65536, "token": TOKEN})

    def test_amazon_keeps_price_without_margin_claim(self):
        mapped = map_research_rows([amazon_row()], request())
        self.assertEqual(mapped["status"], "usable")
        record = mapped["records"][0]
        self.assertEqual(record["price_observation"]["displayed"], "₹249.00")
        self.assertEqual(record["price_observation"]["pack_basis"], "not_verified")
        self.assertNotIn("margin", record)

    def test_amazon_missing_price_not_zero(self):
        for value in (None, ""):
            row = dict(amazon_row(), price=value)
            self.assertIsNone(map_research_rows([row], request())["records"][0]["price_observation"]["displayed"])

    def test_amazon_scope_and_link_conflicts_not_accepted(self):
        for change in ({"categoryUrl": AMAZON_CASES["baby-bestsellers"]}, {"currency": "USD"},
                       {"link": "https://www.amazon.in/dp/B098765432"}, {"categoryUrl": "https://www.amazon.com/gp/bestsellers/kitchen"},
                       {"price": True}, {"status": 503}):
            self.assertEqual(map_research_rows([dict(amazon_row(), **change)], request())["status"], "partial")

    def test_duplicate_and_conflicting_asin(self):
        row = amazon_row()
        mapped = map_research_rows([row, row], request())
        self.assertEqual(len(mapped["records"]), 1)
        self.assertEqual(len(mapped["records"][0]["raw_references"]), 2)
        mapped = map_research_rows([row, dict(row, title="Different product")], request())
        self.assertEqual(len(mapped["records"]), 0)
        self.assertEqual(len(mapped["rejected"]), 2)

    def test_trends_missing_term_and_missing_partial_flags(self):
        mapped = map_research_rows([trends_row()], request("trends", None))
        self.assertEqual(mapped["missing_keywords"], ["packing cubes", "napkin holder"])
        self.assertEqual(mapped["status"], "partial")
        record = mapped["records"][0]
        self.assertIsNone(record["series"][0]["is_partial"])
        self.assertEqual(record["interval_seconds_observed"], [604800])
        self.assertFalse(record["cross_keyword_magnitude_comparable"])

    def test_zero_empty_and_partial_are_distinct(self):
        r = request("trends", None)
        zero = map_research_rows([trends_row(series=[{"date": "1759622400", "value": 0, "isPartial": True}])], r)["records"][0]
        self.assertEqual(zero["series_state"], "all_zero_low_volume_or_no_measurable_interest")
        self.assertTrue(zero["series"][0]["is_partial"])
        empty = map_research_rows([trends_row(series=[])], r)["records"][0]
        self.assertEqual(empty["series_state"], "unavailable")
        self.assertEqual(empty["series"], [])

    def test_trends_wrong_scope_bad_intervals(self):
        for changes in ({"geo": "US"}, {"timeRange": "today 3-m"}, {"category": "Finance"},
                        {"interestOverTime": [{"date": "wrong", "value": 900}]},
                        {"interestOverTime": [{"date": "1759622400", "value": True}]},
                        {"interestOverTime": [{"date": "1759622400", "value": 1}]*2}):
            row = dict(trends_row(), **changes)
            self.assertTrue(map_research_rows([row], request("trends", None))["records"][0]["defects"])

    def test_source_rejects_unrequested_and_nonobject_rows(self):
        self.assertEqual(len(map_research_rows([None, trends_row("different")], request("trends", None))["rejected"]), 2)

    def test_old_dates_and_negative_prices_are_not_accepted(self):
        row = trends_row(series=[{"date": "1000000000", "value": 25}])
        result = map_research_rows([row], request("trends", None))
        self.assertIn("interval_dates_outside_requested_year", result["records"][0]["defects"])
        self.assertEqual(map_research_rows([dict(amazon_row(), price=-10)], request())["status"], "partial")


class SourceProvider(Provider):
    def configure(self, source, prepared):
        spec = SOURCES[source]
        saved = load_json(ROOT / "tests/fixtures/benchmarks" / (source + "_metadata.json"), 524288)
        self.actor, self.build = saved["actor"], saved["build"]
        self.prepared, self.source = prepared, source
        self.cap, self.charge = spec["cap"], Decimal("0.002")
        self.rows = [amazon_row()] if source == "amazon" else [trends_row(t) for t in ("sink organiser", "packing cubes", "napkin holder")]

    def run(self, run_id="runOne"):
        payload = super().run(run_id)
        payload["data"].update(actId=SOURCES[self.source]["actor_id"], buildId=self.prepared.expected_build_id,
                               startedAt=datetime.now(timezone.utc).isoformat())
        return payload

    def open(self, http_request, timeout):
        response = super().open(http_request, timeout)
        if getattr(self, "diagnostic", False) and "/v2/datasets/" in http_request.full_url:
            response.headers["x-apify-pagination-count"] = "0"
            response.headers["x-apify-pagination-total"] = "0"
            if getattr(self, "diagnostic_once", False):
                self.diagnostic = False
        return response


class SourceWorkflowTests(unittest.TestCase):
    setUp = meta_helpers.TrialTests.setUp
    save_facts = meta_helpers.TrialTests.save_facts
    sleep = meta_helpers.TrialTests.sleep
    controller = meta_helpers.TrialTests.controller
    folder = meta_helpers.TrialTests.folder
    bind = meta_helpers.TrialTests.bind
    start = meta_helpers.TrialTests.start
    review_file = meta_helpers.TrialTests.review_file

    def configure(self, source="amazon"):
        self.attempt = source + "-one"
        self.envelope = prepare_source(source, "kitchen-bestsellers" if source == "amazon" else None,
                                       self.attempt, self.now.isoformat())
        atomic_json(self.root / "data/benchmarks/2026-10-04" / self.attempt / "prepared.json", self.envelope)
        self.provider = SourceProvider()
        self.provider.configure(source, prepared_request(self.envelope["request"]))

    def test_amazon_stage_capture_review_and_no_repost(self):
        self.configure()
        with self.journal.locked():
            self.assertEqual(self.start()["verdict"], "Partial")
            self.assertEqual(self.journal.state()[1][self.attempt]["stage"], "amazon")
            self.assertEqual(self.journal.state()[1][self.attempt]["cap"], "0.10")
            self.review_file("QualityPass", None)
            self.assertEqual(self.controller().review(self.attempt)["verdict"], "QualityPass")
            self.controller().check_previous_reviews(self.journal.state()[1])
            self.controller().run(self.attempt)
        self.assertEqual(sum(c[0] == "POST" for c in self.provider.calls), 1)

    def test_trends_stage_and_whole_term_set(self):
        self.configure("trends")
        with self.journal.locked():
            self.assertEqual(self.start()["verdict"], "Partial")
            self.assertEqual(self.journal.state()[1][self.attempt]["stage"], "trends")
            self.assertEqual(self.journal.state()[1][self.attempt]["cap"], "0.03")
            self.review_file("QualityPass", None)
            self.assertEqual(self.controller().review(self.attempt)["verdict"], "QualityPass")

    def test_reviewed_empty_is_isolated_not_passed(self):
        self.configure(); self.provider.rows = []
        with self.journal.locked():
            self.start()
            self.review_file("Partial", None, defects=["Empty list; unavailable, not absence of demand."])
            self.assertEqual(self.controller().review(self.attempt)["verdict"], "Partial")
            self.controller().check_previous_reviews(self.journal.state()[1])
            self.assertEqual(self.journal.state()[1][self.attempt]["review"], "Partial")

    def test_partial_without_reason_does_not_unlock(self):
        self.configure(); self.provider.rows = []
        with self.journal.locked():
            self.start(); self.review_file("Partial", None)
            self.controller().review(self.attempt)
            with self.assertRaisesRegex(SafetyError, "PRIOR_SAMPLE_REVIEW"):
                self.controller().check_previous_reviews(self.journal.state()[1])

    def test_changed_review_or_mapping_does_not_unlock(self):
        self.configure()
        with self.journal.locked():
            self.start(); self.review_file("QualityPass", None); self.controller().review(self.attempt)
            record = load_json(self.folder / "operator_review.json"); record["scope_checked"] = False
            atomic_json(self.folder / "operator_review.json", record)
            with self.assertRaisesRegex(SafetyError, "PRIOR_SAMPLE_REVIEW"):
                self.controller().check_previous_reviews(self.journal.state()[1])

    def test_lost_receipt_recovers_exact_input_once(self):
        self.configure(); self.provider.lost = True
        with self.journal.locked():
            self.start()
            self.provider.recovery_items = [{"id": "runOne", "actId": SOURCES["amazon"]["actor_id"]}]
            self.assertEqual(self.controller().run(self.attempt, resume_only=True)["verdict"], "Partial")
        self.assertEqual(sum(c[0] == "POST" for c in self.provider.calls), 1)

    def test_ambiguous_receipt_never_reposts(self):
        self.configure(); self.provider.lost = True
        with self.journal.locked():
            self.start()
            self.provider.recovery_items = [{"id": i, "actId": SOURCES["amazon"]["actor_id"]} for i in ("runOne", "runTwo")]
            self.provider.inputs["storerunTwo"] = self.provider.inputs["storerunOne"]
            result = self.controller().run(self.attempt, resume_only=True)
            self.assertEqual(result["code"], "RECOVERY_UNIQUE_MATCH_NOT_ESTABLISHED")
        self.assertEqual(sum(c[0] == "POST" for c in self.provider.calls), 1)

    def test_cli_rejects_cross_source_flags(self):
        with patch.object(cli, "ROOT", self.root), patch("builtins.print"):
            for flags in (["prepare", "--source", "trends", "--term", "anything"],
                          ["prepare", "--source", "amazon"], ["status", "--source", "amazon"]):
                self.assertEqual(cli.main(flags), 2)

    def test_amazon_explicit_source_error_preserved_with_zero_provider_count(self):
        self.configure()
        self.provider.rows = [{"categoryUrl": AMAZON_CASES["kitchen-bestsellers"], "status": 200, "errorMessage": "Empty page structure"}]
        self.provider.diagnostic = True
        with self.journal.locked():
            self.assertEqual(self.start()["verdict"], "Partial")
            manifest = load_json(self.folder / "capture_manifest.json")
            self.assertEqual((manifest["returned_total"], manifest["captured_count"]), (0, 1))
            self.assertEqual(load_json(self.folder / "captured_rows.json"), self.provider.rows)
            self.review_file("Partial", None, defects=["Provider empty-page diagnostic, no product data."])
            self.controller().review(self.attempt)
            self.controller().check_previous_reviews(self.journal.state()[1])

    def test_arbitrary_count_mismatch_is_not_source_diagnostic(self):
        self.configure(); self.provider.diagnostic = True
        with self.journal.locked():
            self.assertEqual(self.start()["code"], "DATASET_PAGINATION_INCONSISTENT")

    def test_delayed_charge_is_append_only_and_idempotent(self):
        self.configure()
        with self.journal.locked():
            self.start()
            original = self.journal.path.read_bytes()
            self.provider.charge = Decimal("0.07805")
            self.controller().refresh_costs()
            self.assertTrue(self.journal.path.read_bytes().startswith(original))
            self.assertEqual(self.journal.state()[1][self.attempt]["charge"], "0.07805000")
            self.assertEqual(load_json(self.folder / "result.json")["actor_actual_usd"], "0.07805000")
            snapshot = self.journal.path.read_bytes()
            self.controller().refresh_costs()
            self.assertEqual(self.journal.path.read_bytes(), snapshot)
        self.assertEqual(sum(c[0] == "POST" for c in self.provider.calls), 1)

    def test_billing_decrease_never_releases_funds_automatically(self):
        self.configure()
        with self.journal.locked():
            self.start(); original = self.journal.path.read_bytes()
            self.provider.charge = Decimal("0.001")
            with self.assertRaisesRegex(SafetyError, "COST_DECREASE"):
                self.controller().refresh_costs()
            self.assertEqual(self.journal.path.read_bytes(), original)

    def test_delayed_overrun_is_recorded_and_blocks_new_spending(self):
        self.configure()
        with self.journal.locked():
            self.start(); self.provider.charge = Decimal("0.2")
            self.controller().refresh_costs()
            self.assertEqual(self.journal.state()[1][self.attempt]["charge"], "0.20000000")
            self.assertEqual(load_json(self.folder / "result.json")["code"], "PROVIDER_CAP_OVERRUN_REVIEW_REQUIRED")
            with self.assertRaisesRegex(SafetyError, "PRIOR_SAMPLE_REVIEW"):
                self.controller().check_previous_reviews(self.journal.state()[1])

    def test_zero_counter_lag_corroborates_same_saved_body_once(self):
        self.configure(); self.provider.diagnostic = self.provider.diagnostic_once = True
        with self.journal.locked():
            self.assertEqual(self.start()["code"], "DATASET_PAGINATION_INCONSISTENT")
            self.assertEqual(self.controller().run(self.attempt, resume_only=True)["verdict"], "Partial")
            self.controller().run(self.attempt, resume_only=True)
            self.assertTrue((self.folder / "pagination_refresh.json").exists())
        self.assertEqual(sum("/v2/datasets/" in c[1] for c in self.provider.calls), 2)
        self.assertEqual(sum(c[0] == "POST" for c in self.provider.calls), 1)

    def test_persistent_zero_counter_lag_does_not_repeat_forever(self):
        self.configure(); self.provider.diagnostic = True
        with self.journal.locked():
            self.start()
            for _ in range(3):
                self.assertEqual(self.controller().run(self.attempt, resume_only=True)["code"], "DATASET_PAGINATION_INCONSISTENT")
        self.assertEqual(sum("/v2/datasets/" in c[1] for c in self.provider.calls), 2)

    def test_corroboration_cannot_replace_changed_product_bytes(self):
        self.configure(); self.provider.diagnostic = self.provider.diagnostic_once = True
        with self.journal.locked():
            self.start(); self.provider.rows[0]["title"] = "Different product after completion"
            result = self.controller().run(self.attempt, resume_only=True)
            self.assertEqual(result["code"], "SAVED_PAGE_CONFLICT")
            self.assertIsNone(self.journal.state()[1][self.attempt]["capture"])

    def test_failed_trends_with_saved_reason_allows_independent_trials(self):
        self.configure("trends"); self.provider.status = "FAILED"
        self.provider.rows = []; self.provider.charge = Decimal("0")
        with self.journal.locked():
            self.start()
            self.review_file("Failed", None, defects=["Fictional queue permission failure; no demand data."])
            self.controller().review(self.attempt)
            self.controller().check_previous_reviews(self.journal.state()[1])


if __name__ == "__main__":
    unittest.main()
