"""Real single-leaf QC adapters, synthetic owned HTTP/browser originals only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from product_radar.catalogue_discovery import advance_discovery_cycle, discovery_wave_readiness, prepare_next_discovery_wave
from product_radar.models import SafetyError
from product_radar.quickcommerce import QuickCommerceClient, prepare_query
from product_radar.quickcommerce_browser import create_quickcommerce_handlers
from product_radar.research_memory import ResearchMemory
from product_radar.source_acquisitions import ACQUISITION_KIND
from product_radar.source_coverage import execute_source_run, installed_source_handlers, prepare_source_tasks
from product_radar.storage import Journal, atomic_json, load_json
from product_radar.work_turns import bind_work_turn, close_work_turn, open_work_turn
from test_catalogue_expansion import fixture
from test_quickcommerce import LOCATION, Opener, Response, SECRET, payload
from test_supplier_discovery import CONCEPT

NOW = "2026-10-09T10:00:00+00:00"


class SharedQuickCommerceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.native_calls = []; self.now = NOW
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.memory.put("concepts", "sink", deepcopy(CONCEPT))
        self.catalogue = fixture(); self.catalogue["families"][0]["phrases"][0]["source"] = "quickcommerce"
        self.request = prepare_query("blinkit", "sink caddy", LOCATION)
        self.opener = Opener(Response(json.dumps(payload(self.request)).encode()))
        self.client = QuickCommerceClient(SECRET, opener=self.opener)

    def native(self, url, expression, folder):
        self.native_calls.append(url)
        value = {"state": "captured", "navigation_started": True, "navigation_status": 429,
                 "owned_process_exited": True, "completed_at": self.now, "retry_after": "7200",
                 "snapshot": {"url": url, "title": "Synthetic public page", "text": "Too many requests", "location_widgets": []}}
        atomic_json(Path(folder) / "native_result.json", value)
        return value

    def handlers(self, funded=True):
        handlers = create_quickcommerce_handlers(self.memory, self.client if funded else None,
            native_capture=self.native, respect_access_waits=True, shared_acquisitions=True)
        return {"blinkit": handlers["blinkit"]}

    def prepare(self, handlers, *, run="checks", programme="programme", location=LOCATION):
        self.run = run
        prepare_source_tasks(self.memory, [{"route": "blinkit", "request": {"query": "sink caddy", "location": deepcopy(location)},
            "concept_ids": ["sink"]}], run_id=run, as_of=self.now)
        self.wave = prepare_next_discovery_wave(self.memory, programme, self.catalogue, handlers=handlers,
            location=location, as_of=self.now)

    def checks(self, handlers):
        with patch("product_radar.quickcommerce.utc_now", return_value=self.now), patch("product_radar.quickcommerce_browser.utc_now", return_value=self.now):
            return execute_source_run(self.memory, self.run, handlers=handlers, as_of=self.now, shared_marketplace=True,
                bounded_call=lambda callback, context, timeout: callback(context))

    def discovery(self, handlers):
        turn = open_work_turn(self.memory, self.wave["programme_id"], at=self.now)
        try:
            with bind_work_turn(self.memory, turn["id"]), patch("product_radar.quickcommerce.utc_now", return_value=self.now), patch("product_radar.quickcommerce_browser.utc_now", return_value=self.now):
                result = advance_discovery_cycle(self.memory, self.wave["id"], handlers=handlers, as_of=self.now,
                    shared_marketplace=True, admission=lambda *_args, **_options: None)
            return result
        finally:
            self.last_turn = close_work_turn(self.memory, turn["id"], at=self.now, reason="Synthetic finite QC direction")

    def owners(self):
        return {rid: row for rid, row in self.memory.snapshot()["batch_runs"].items() if row.get("kind") == ACQUISITION_KIND}

    def test_delayed_funded_completion_uses_actual_time_across_iso_offsets(self):
        self.now = "2026-10-09T15:30:00+05:30"
        completed, committed = "2026-10-09T10:00:03+00:00", "2026-10-09T10:00:05+00:00"
        handlers = self.handlers(); self.prepare(handlers)
        with patch("product_radar.quickcommerce.utc_now", return_value=completed), \
             patch("product_radar.quickcommerce_browser.utc_now", return_value=committed):
            result = execute_source_run(self.memory, self.run, handlers=handlers, as_of=self.now,
                shared_marketplace=True, bounded_call=lambda callback, context, timeout: callback(context))
        self.assertEqual(result["counts"], {"acquired": 1})
        state = self.memory.snapshot(); owner = next(iter(self.owners().values()))
        response = state["source_checks"][owner["active_response_id"]]
        self.assertEqual(response["observed_at"], completed)
        self.assertEqual(response["recorded_at"], committed)
        self.assertEqual(len(self.opener.calls), 1)
        self.assertEqual(self.native_calls, [])

    def test_funded_check_then_direction_shares_exact_http_original_not_public_browsing(self):
        handlers = self.handlers(); self.prepare(handlers)
        self.assertEqual(self.checks(handlers)["counts"], {"acquired": 1})
        self.assertEqual(self.discovery(handlers)["external_handler_calls_this_round"], 0)
        self.assertEqual(len(self.opener.calls), 1); self.assertEqual(self.native_calls, [])
        self.assertEqual(self.last_turn["counters"]["source_callbacks"], 0)
        owner = next(iter(self.owners().values())); state = self.memory.snapshot()
        self.assertEqual(owner["binding"]["request"]["transport"], "funded-http")
        self.assertEqual(owner["binding"]["request"]["location"], self.request["location"])
        self.assertEqual(owner["binding"]["collection_channel"], "funded_provider")
        self.assertEqual(owner["binding"]["incremental_collection_fee"], "unknown")
        response = state["source_checks"][owner["active_response_id"]]
        self.assertTrue(response["complete"]); self.assertEqual(response["observed_at"], NOW)
        self.assertEqual(response["expense_proof"]["provider_credits_expected"], 1)
        self.assertEqual(len(state["concepts"]), 1); self.assertEqual(state["market_reviews"], {})

    def test_direction_then_product_check_and_restart_reuses_original(self):
        handlers = self.handlers(); self.prepare(handlers)
        self.assertEqual(self.discovery(handlers)["external_handler_calls_this_round"], 1)
        self.memory = ResearchMemory(self.memory.root, Journal(self.root / "finance"), evidence_type="synthetic")
        self.assertEqual(self.checks(self.handlers())["handler_calls"], 0)
        self.assertEqual(len(self.opener.calls), 1); self.assertEqual(len(self.owners()), 1)

    def test_missing_location_is_named_without_fake_questions_or_calls(self):
        before = self.memory.snapshot()
        hint = discovery_wave_readiness(self.memory, "programme", self.catalogue, handlers=self.handlers(),
            as_of=self.now)
        self.assertFalse(hint["new_wave_ready"])
        self.assertEqual({r["route"] for r in hint["preparation_gaps"]}, {"blinkit", "zepto", "instamart"})
        self.assertEqual({r["reason"] for r in hint["preparation_gaps"]}, {"QUICKCOMMERCE_LOCATION_REQUIRED"})
        self.assertEqual(before, self.memory.snapshot()); self.assertEqual(self.opener.calls, [])

    def test_location_validation_phrase_keeps_its_intended_purpose(self):
        self.catalogue["families"][0]["phrases"][0]["purpose"] = "validation"
        self.prepare(self.handlers())
        questions = [self.memory.snapshot()["query_tasks"][r["id"]] for r in self.wave["planned_actions"]]
        self.assertEqual({r["purpose"] for r in questions}, {"validation"})
        self.assertEqual({r["route"] for r in questions}, {"blinkit", "zepto", "instamart"})
        self.assertEqual(self.opener.calls, [])

    def test_public_refusal_does_not_suppress_new_healthy_funded_leaf(self):
        public = self.handlers(False); self.prepare(public)
        self.assertEqual(self.checks(public)["counts"], {"blocked": 1})
        public_owner = next(iter(self.owners()))
        self.assertEqual(self.checks(self.handlers())["counts"], {"acquired": 1})
        self.assertEqual(len(self.native_calls), 1); self.assertEqual(len(self.opener.calls), 1)
        self.assertEqual(len(self.owners()), 2)
        self.assertEqual(self.owners()[public_owner]["binding"]["request"]["transport"], "public-browser")

    def test_public_direction_read_remains_blocked_not_empty_or_a_fake_product(self):
        handlers = self.handlers(False); self.prepare(handlers); self.discovery(handlers)
        self.assertEqual(len(self.native_calls), 1); self.assertEqual(self.opener.calls, [])
        self.assertEqual(self.memory.snapshot()["observations"], {})
        self.assertEqual(len(self.memory.snapshot()["concepts"]), 1)
        self.assertEqual(next(iter(self.owners().values()))["state"], "failed_access")

    def test_interrupted_funded_http_never_reissues_from_either_consumer(self):
        self.opener.error = TimeoutError("Synthetic unknown original HTTP")
        handlers = self.handlers(); self.prepare(handlers)
        self.checks(handlers); self.opener.error = None
        self.discovery(handlers); self.checks(handlers)
        self.assertEqual(len(self.opener.calls), 1)
        owner = next(iter(self.owners().values()))
        self.assertFalse(self.memory.snapshot()["source_checks"][owner["active_response_id"]]["complete"])

    def test_import_failure_recovers_saved_original_without_spending_another_credit(self):
        handlers = self.handlers(); self.prepare(handlers)
        with patch("product_radar.quickcommerce.save_capture", side_effect=OSError("Synthetic import failure")):
            self.checks(handlers)
        self.discovery(handlers)
        self.assertEqual(len(self.opener.calls), 1); self.assertEqual(len(self.memory.snapshot()["observations"]), 1)

    def test_mismatched_provider_location_is_not_empty_or_positive_capture(self):
        bad = payload(self.request); bad["data"]["lat"] = 20
        self.opener.response = Response(json.dumps(bad).encode())
        handlers = self.handlers(); self.prepare(handlers)
        self.checks(handlers); self.discovery(handlers)
        self.assertEqual(len(self.opener.calls), 1); self.assertEqual(self.memory.snapshot()["observations"], {})
        owner = next(iter(self.owners().values())); response = self.memory.snapshot()["source_checks"][owner["active_response_id"]]
        self.assertFalse(response["complete"]); self.assertNotEqual(owner["state"], "completed_empty")

    def test_true_provider_empty_is_complete_without_market_vote(self):
        self.opener.response = Response(json.dumps(payload(self.request, [])).encode())
        handlers = self.handlers(); self.prepare(handlers)
        self.discovery(handlers)
        self.assertEqual(self.checks(handlers)["counts"], {"completed_no_results": 1})
        self.assertEqual(len(self.opener.calls), 1); self.assertEqual(self.memory.snapshot()["market_reviews"], {})

    def test_installed_factory_preserves_shared_original_reconciler(self):
        handlers = installed_source_handlers(self.memory, quickcommerce_client=self.client,
            quickcommerce_options={"shared_acquisitions": True, "native_capture": self.native})
        # Isolate this installed leaf. The full factory also installs Zepto
        # and Instamart, whose separate healthy questions may legitimately run.
        handlers = {"blinkit": handlers["blinkit"]}
        self.prepare(handlers)
        with patch("product_radar.quickcommerce.save_capture", side_effect=OSError("Synthetic import failure")):
            self.checks(handlers)
        self.discovery(handlers)
        self.assertEqual(len(self.opener.calls), 1)


if __name__ == "__main__":
    unittest.main()
