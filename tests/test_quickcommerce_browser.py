"""Free-attempt/funded-fallback fixtures; no real network or charged calls."""

from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from product_radar.models import SafetyError, digest, timestamp, utc_now
from product_radar.quickcommerce import QuickCommerceClient, prepare_query
from product_radar.quickcommerce_browser import (capture_free_probe, create_quickcommerce_handlers,
                                               note_route_failure, public_search_url)
from product_radar.research_memory import ResearchMemory
from product_radar.source_coverage import execute_source_run, prepare_source_tasks, source_coverage
from product_radar.storage import Journal, atomic_json, load_json
from test_quickcommerce import LOCATION, Opener, Response, SECRET, payload
from test_supplier_discovery import CONCEPT


class Native:
    def __init__(self, status=403, actual=True, retry_after=None):
        self.status, self.actual, self.calls, self.retry_after = status, actual, [], retry_after

    def __call__(self, url, expression, folder):
        self.calls.append(url)
        value = {"state": "captured" if self.actual else "failed", "navigation_started": self.actual,
                 "navigation_status": self.status, "completed_at": utc_now(), "owned_process_exited": True,
                 "snapshot": {"url": url, "title": "Fixture page", "text": "Cities served: Mumbai, Delhi.",
                              "location_widgets": []}}
        if self.retry_after is not None:
            value["retry_after"] = self.retry_after
        atomic_json(Path(folder) / "native_result.json", value)
        return value


class QuickCommerceBrowserTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.memory.put("concepts", "paper", deepcopy(CONCEPT))
        self.native = Native()
        self.request = prepare_query("blinkit", "drain strainer", LOCATION)

    def context(self, acquisition="free-example", platform="blinkit", query="drain strainer"):
        return {"route": platform, "acquisition_id": acquisition, "concept_ids": ["paper"],
                "description_hashes": {"paper": digest(CONCEPT["description"])},
                "request": {"query": query, "location": deepcopy(LOCATION)}}

    def client(self):
        opener = Opener(Response(json.dumps(payload(self.request)).encode()))
        return QuickCommerceClient(SECRET, opener=opener), opener

    def test_three_ordinary_destinations_are_bounded_queries_not_order_routes(self):
        urls = [public_search_url(platform, "food wrapping paper", LOCATION)
                for platform in ("blinkit", "zepto", "instamart")]
        self.assertEqual(len(set(urls)), 3)
        self.assertTrue(all("food+wrapping+paper" in url for url in urls))
        self.assertTrue(all("checkout" not in url for url in urls))

    def test_footer_city_names_do_not_confirm_location_or_empty_products(self):
        native = Native(status=200)
        result = capture_free_probe(self.memory.root, "readable", self.request, native_capture=native)
        self.assertEqual(result["record"]["state"], "insufficient")
        self.assertFalse(result["record"]["location_established"])
        self.assertTrue(result["record"]["dispatch_started"])
        self.assertNotIn("products", result["record"])

    def test_blocked_free_route_is_not_empty_and_next_product_is_not_an_actual_attempt(self):
        first = capture_free_probe(self.memory.root, "first", self.request, native_capture=self.native)
        second_request = prepare_query("blinkit", "another product", LOCATION)
        second = capture_free_probe(self.memory.root, "second", second_request, native_capture=self.native)
        self.assertEqual(first["record"]["state"], "blocked")
        self.assertEqual(second["record"]["state"], "not_attempted")
        self.assertFalse(second["record"]["dispatch_started"])
        self.assertEqual(len(self.native.calls), 1)

    def test_existing_live_pilot_barrier_can_guard_platform_without_cloning_product_attempt(self):
        pilot = self.root / "pilot.json"
        atomic_json(pilot, {"requested_url": public_search_url("blinkit", "food paper", LOCATION),
            "native": {"navigation_started": True, "navigation_status": 403, "completed_at": utc_now()}})
        note_route_failure(self.memory.root, "blinkit", pilot)
        result = capture_free_probe(self.memory.root, "next-product", self.request, native_capture=self.native)
        self.assertFalse(result["record"]["dispatch_started"])
        self.assertEqual(self.native.calls, [])

    def test_real_retry_after_seconds_outlasts_the_normal_shorter_wait(self):
        native = Native(status=429, retry_after="7200")
        first = capture_free_probe(self.memory.root, "long-wait", self.request, native_capture=native)
        observed = first["record"]["observed_at"]
        later = (timestamp(observed) + timedelta(seconds=1900)).isoformat()
        with patch("product_radar.quickcommerce_browser.utc_now", return_value=later):
            second = capture_free_probe(self.memory.root, "other-query",
                prepare_query("blinkit", "food wrapping paper", LOCATION), native_capture=native)
        self.assertIs(second["record"]["dispatch_started"], False)
        self.assertEqual(first["record"]["retry_after"], "7200")
        self.assertEqual(len(native.calls), 1)
        health = load_json(self.memory.root / "quickcommerce_free/route_guards/blinkit/health.json")
        self.assertEqual(timestamp(health["blocked_until"]) - timestamp(observed), timedelta(seconds=7200))

    def test_real_http_date_retry_after_and_closed_due_probe(self):
        from email.utils import format_datetime
        at = "2026-10-09T10:00:00Z"
        due = "2026-10-09T14:00:00Z"
        native = Native(status=429, retry_after=format_datetime(timestamp(due), usegmt=True))
        with patch("product_radar.quickcommerce_browser.utc_now", return_value=at), \
                patch(__name__ + ".utc_now", return_value=at):
            capture_free_probe(self.memory.root, "date-wait", self.request, native_capture=native)
        healthy = Native(status=200)
        with patch("product_radar.quickcommerce_browser.utc_now", return_value=due), \
                patch(__name__ + ".utc_now", return_value=due):
            result = capture_free_probe(self.memory.root, "closed-due-probe", self.request, native_capture=healthy)
        self.assertTrue(result["record"]["dispatch_started"])
        self.assertEqual(len(healthy.calls), 1)
        self.assertEqual(result["record"]["state"], "insufficient")

    def test_due_pilot_without_process_exit_proof_does_not_launch_again(self):
        pilot = self.root / "pilot-unknown-exit.json"
        at = "2026-10-09T10:00:00Z"
        atomic_json(pilot, {"requested_url": public_search_url("blinkit", "food paper", LOCATION),
            "native": {"navigation_started": True, "navigation_status": 429, "completed_at": at}})
        note_route_failure(self.memory.root, "blinkit", pilot, cooldown_seconds=1)
        with patch("product_radar.quickcommerce_browser.utc_now", return_value="2026-10-09T11:00:00Z"):
            result = capture_free_probe(self.memory.root, "unknown-exit", self.request, native_capture=self.native)
        self.assertIs(result["record"]["dispatch_started"], False)
        self.assertIn("no confirmed process exit", result["record"]["reason"])
        self.assertEqual(self.native.calls, [])

    def test_guard_original_hash_rechecked_before_any_new_browser(self):
        first = capture_free_probe(self.memory.root, "original", self.request, native_capture=self.native)
        original = load_json(first["reference"])
        original["record"]["native"]["navigation_status"] = 200
        atomic_json(first["reference"], original)
        with self.assertRaisesRegex(SafetyError, "QUICKCOMMERCE_FREE_GUARD_INVALID"):
            capture_free_probe(self.memory.root, "new-read", self.request, native_capture=self.native)
        self.assertEqual(len(self.native.calls), 1)

    def test_saved_original_replays_before_an_unrelated_damaged_guard(self):
        first = capture_free_probe(self.memory.root, "saved-before-guard", self.request, native_capture=self.native)
        health = self.memory.root / "quickcommerce_free/route_guards/blinkit/health.json"
        atomic_json(health, {"wrong": "local guard"})
        second = capture_free_probe(self.memory.root, "saved-before-guard", self.request,
            native_capture=self.native, saved_only=True)
        self.assertEqual(first["record"], second["record"])
        self.assertEqual(len(self.native.calls), 1)

    def test_shorter_late_pilot_cannot_shorten_an_existing_longer_refusal(self):
        first = capture_free_probe(self.memory.root, "longer-original", self.request,
            native_capture=Native(status=429, retry_after="7200"))
        pilot = self.root / "shorter-later-pilot.json"
        at = (timestamp(first["record"]["observed_at"]) + timedelta(seconds=10)).isoformat()
        atomic_json(pilot, {"requested_url": public_search_url("blinkit", "food paper", LOCATION),
            "native": {"navigation_started": True, "navigation_status": 429, "completed_at": at,
                       "owned_process_exited": True, "retry_after": "1"}})
        kept = note_route_failure(self.memory.root, "blinkit", pilot)
        self.assertEqual(kept["reference"], first["reference"])

    def test_invalid_retry_header_is_not_copied_into_guard(self):
        first = capture_free_probe(self.memory.root, "invalid-header", self.request,
            native_capture=Native(status=429, retry_after="100\nsecret"))
        self.assertNotIn("retry_after", first["record"])
        health = load_json(self.memory.root / "quickcommerce_free/route_guards/blinkit/health.json")
        self.assertIsNone(health["retry_after"])

    def test_public_replay_and_saved_only_reconciliation_make_no_native_requests(self):
        first = capture_free_probe(self.memory.root, "saved", self.request, native_capture=self.native)
        second = capture_free_probe(self.memory.root, "saved", self.request, native_capture=self.native, saved_only=True)
        self.assertEqual(first["record"], second["record"])
        self.assertEqual(len(self.native.calls), 1)
        with self.assertRaisesRegex(SafetyError, "QUICKCOMMERCE_SAVED_CAPTURE_REQUIRED"):
            capture_free_probe(self.memory.root, "missing", self.request, native_capture=self.native, saved_only=True)

    def test_prelaunch_failure_is_not_a_platform_block_or_request(self):
        result = capture_free_probe(self.memory.root, "startup", self.request, native_capture=Native(actual=False))
        self.assertEqual(result["record"]["state"], "not_attempted")
        self.assertFalse(result["record"]["dispatch_started"])

    def test_missing_navigation_proof_remains_uncertain_and_saved_replay_is_zero_call(self):
        native = Native(actual=None)
        handler = create_quickcommerce_handlers(self.memory, native_capture=native)["blinkit"]
        first = handler(self.context("uncertain"))
        self.assertIsNone(first["dispatch_started"])
        self.assertEqual(first["state"], "failed")
        self.assertEqual(first["target_results"][0]["state"], "failed")
        replay = handler(dict(self.context("uncertain"), reconciling=True))
        self.assertEqual(first, replay)
        self.assertEqual(len(native.calls), 1)

    def test_saved_native_result_rebuilds_interrupted_probe_without_a_browser(self):
        folder = self.memory.root / "quickcommerce_free/acquisitions/interrupted/native"
        self.native(public_search_url("blinkit", self.request["query"], LOCATION), "fixture", folder)
        result = capture_free_probe(self.memory.root, "interrupted", self.request,
                                    native_capture=self.native, saved_only=True)
        self.assertTrue(result["record"]["dispatch_started"])
        self.assertEqual(result["record"]["state"], "blocked")
        self.assertEqual(len(self.native.calls), 1)

    def test_unknown_native_exception_does_not_invent_prelaunch_proof(self):
        def interrupted(*_args):
            raise RuntimeError("Synthetic native interruption")
        result = capture_free_probe(self.memory.root, "throwing", self.request, native_capture=interrupted)
        self.assertIsNone(result["record"]["dispatch_started"])
        self.assertEqual(result["record"]["state"], "failed")

    def test_default_native_transport_receives_an_existing_owned_output_folder(self):
        def native(url, expression, folder):
            self.assertTrue(Path(folder).is_dir())
            return self.native(url, expression, folder)
        with patch("product_radar.native_public_browser.native_snapshot", side_effect=native):
            result = capture_free_probe(self.memory.root, "default-native", self.request)
        self.assertTrue(result["record"]["dispatch_started"])

    def test_funded_fallback_and_second_replay_call_each_transport_only_once(self):
        client, opener = self.client()
        handler = create_quickcommerce_handlers(self.memory, client, native_capture=self.native)["blinkit"]
        first = handler(self.context())
        second = handler(dict(self.context(), reconciling=True))
        self.assertEqual(first["capture_ids"], second["capture_ids"])
        self.assertEqual(first["free_route_state"], "blocked")
        self.assertEqual(len(self.native.calls), 1)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(first["target_results"][0]["state"], "interpretation_pending")
        self.assertNotIn(SECRET, Path(first["reference"]).read_text())

    def test_funded_response_retained_before_canonical_import_is_reused_after_interruption(self):
        client, opener = self.client()
        handler = create_quickcommerce_handlers(self.memory, client, native_capture=self.native)["blinkit"]
        with patch("product_radar.quickcommerce.save_capture", side_effect=SafetyError("SYNTHETIC_WRITER_BUSY")):
            with self.assertRaisesRegex(SafetyError, "SYNTHETIC_WRITER_BUSY"):
                handler(self.context())
        result = handler(dict(self.context(), reconciling=True))
        self.assertEqual(result["state"], "usable")
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(len(self.native.calls), 1)

    def test_funded_prelaunch_failure_does_not_erase_actual_public_navigation(self):
        client, opener = self.client()
        attempts = []
        def dispatch():
            attempts.append(True)
            if len(attempts) == 2:
                raise SafetyError("RUNNER_ALREADY_ACTIVE")
        handler = create_quickcommerce_handlers(self.memory, client, native_capture=self.native)["blinkit"]
        context = dict(self.context("partial-attempt"), record_dispatch=dispatch)
        first = handler(context)
        self.assertIs(first["dispatch_started"], True)
        self.assertIs(first["stages"]["public"]["dispatch_started"], True)
        self.assertIs(first["stages"]["funded"]["dispatch_started"], False)
        self.assertEqual(first["target_results"][0]["state"], "failed")
        self.assertEqual(opener.calls, [])
        second = handler(dict(context, reconciling=True))
        self.assertEqual(first["reference"], second["reference"])
        self.assertEqual(first["stages"], second["stages"])
        self.assertEqual(len(self.native.calls), 1)
        self.assertEqual(len(attempts), 2)

    def test_lost_funded_response_is_not_blindly_charged_again(self):
        class Lost:
            calls = 0
            def search(self, _request, *, on_dispatch=None):
                self.calls += 1
                on_dispatch()
                raise RuntimeError("Synthetic lost response")
        client = Lost()
        handler = create_quickcommerce_handlers(self.memory, client, native_capture=self.native)["blinkit"]
        with self.assertRaises(RuntimeError):
            handler(self.context())
        with self.assertRaisesRegex(SafetyError, "QUICKCOMMERCE_FUNDED_RESPONSE_UNCERTAIN"):
            handler(dict(self.context(), reconciling=True))
        self.assertEqual(client.calls, 1)

    def test_failed_free_route_and_healthy_funded_capture_share_real_controller(self):
        client, opener = self.client()
        handlers = create_quickcommerce_handlers(self.memory, client, native_capture=self.native)
        prepare_source_tasks(self.memory, [{"route": "blinkit", "request": self.context()["request"],
                            "concept_ids": ["paper"]}], run_id="free-fallback")
        execute_source_run(self.memory, "free-fallback", handlers=handlers)
        execute_source_run(self.memory, "free-fallback", handlers=handlers)
        row = next(row for row in source_coverage(self.memory, concept_ids=["paper"])["rows"]
                   if row["route"] == "blinkit")
        self.assertTrue(row["actual_attempt"])
        self.assertEqual(row["state"], "interpretation_pending")
        self.assertEqual(len(opener.calls), 1)

    def test_no_funded_client_keeps_barrier_honest_and_does_not_stop_other_platform(self):
        handlers = create_quickcommerce_handlers(self.memory, native_capture=self.native)
        first = handlers["blinkit"](self.context())
        other = handlers["zepto"](self.context("other-platform", "zepto"))
        self.assertEqual(first["state"], "blocked")
        self.assertEqual(other["state"], "blocked")
        self.assertEqual(first["observation_ids"], [])
        self.assertEqual(len(self.native.calls), 2)


if __name__ == "__main__":
    unittest.main()
