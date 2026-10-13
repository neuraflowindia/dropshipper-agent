"""Installed public/funded boundaries through the actual finite controller.

All source transports are temporary-file fixtures. No real website is read.
"""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import io
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from product_radar.models import SafetyError, digest, timestamp
from product_radar.quickcommerce import QuickCommerceClient
from product_radar.programme import _source_turn_readiness
from product_radar.quickcommerce_browser import create_quickcommerce_handlers, note_route_failure, public_search_url
from product_radar.research_memory import key
from product_radar.source_coverage import (execute_source_run, installed_source_handlers, prepare_source_tasks,
                                         route_registry, source_access_context)
from product_radar.storage import atomic_json, load_json
from product_radar.work_turns import DEFAULT_POLICY, active_work_turn, bind_work_turn, open_work_turn
from test_quickcommerce import LOCATION, Opener, SECRET
import test_quickcommerce_browser as browser_fixtures
Native = browser_fixtures.Native

NOW = "2026-10-09T10:00:00+00:00"


class QuickCommerceAccessControllerTests(unittest.TestCase):
    setUp = browser_fixtures.QuickCommerceBrowserTests.setUp
    context = browser_fixtures.QuickCommerceBrowserTests.context
    client = browser_fixtures.QuickCommerceBrowserTests.client

    def handlers(self, client=None, native=None):
        return installed_source_handlers(self.memory, quickcommerce_client=client,
            quickcommerce_options={"native_capture": native or self.native, "respect_access_waits": True})

    def plan(self, query="drain strainer", route="blinkit", run_id="public-wait"):
        result = prepare_source_tasks(self.memory, [{"route": route,
            "request": {"query": query, "location": deepcopy(LOCATION)}, "concept_ids": ["paper"]}],
            run_id=run_id, as_of=NOW)
        result["task_ids"] = [task_id for task_id in result["task_ids"]
            if self.memory.snapshot()["query_tasks"][task_id]["route"] == route
            and self.memory.snapshot()["query_tasks"][task_id]["request"]["query"] == query]
        return result

    def run_at(self, handlers, at=NOW, *, runner=None, run_id="public-wait"):
        with patch("product_radar.quickcommerce_browser.utc_now", return_value=at), \
             patch("product_radar.quickcommerce.utc_now", return_value=at), \
             patch("product_radar.source_coverage.utc_now", return_value=at), \
             patch("test_quickcommerce_browser.utc_now", return_value=at):
            return execute_source_run(self.memory, run_id, handlers=handlers, as_of=at,
                bounded_call=runner or (lambda callback, context, timeout: callback(context)))

    def task(self, plan):
        return self.memory.snapshot()["query_tasks"][plan["task_ids"][0]]

    def test_actual_outer_retry_and_route_wait_keep_original_two_hour_deadline(self):
        native = Native(status=429, retry_after="7200")
        plan = self.plan(); handlers = self.handlers(native=native)
        self.assertEqual(self.run_at(handlers)["handler_calls"], 1)
        task = self.task(plan)
        self.assertEqual(timestamp(task["next_retry_at"]) - timestamp(NOW), timedelta(hours=2))
        run = self.memory.snapshot()["batch_runs"][key("source-run", "public-wait")]
        self.assertEqual(run["route_pauses"]["blinkit"]["next_retry_at"], task["next_retry_at"])
        for offset in (301, 1801, 7199):
            at = (timestamp(NOW) + timedelta(seconds=offset)).isoformat()
            result = self.run_at(handlers, at)
            self.assertEqual(result["handler_calls"], 0)
        after = self.task(plan)
        self.assertEqual((after["tries"], after["prelaunch_failures"], after["generation"]), (1, 0, 1))
        self.assertEqual(len(native.calls), 1)

    def test_guard_before_first_task_spends_no_callback_or_failure(self):
        pilot = self.root / "public-original.json"
        atomic_json(pilot, {"requested_url": public_search_url("blinkit", "food paper", LOCATION),
            "native": {"navigation_started": True, "navigation_status": 429, "completed_at": NOW,
                       "owned_process_exited": True, "retry_after": "7200"}})
        note_route_failure(self.memory.root, "blinkit", pilot)
        plan = self.plan(); policy = dict(DEFAULT_POLICY, source_callbacks=1, exploration_slots=0)
        turn = open_work_turn(self.memory, "programme", at=NOW, policy=policy)
        with bind_work_turn(self.memory, turn["id"]):
            result = self.run_at(self.handlers())
            self.assertEqual(active_work_turn(self.memory)["counters"]["source_callbacks"], 0)
        self.assertEqual(result["handler_calls"], 0)
        task = self.task(plan)
        self.assertEqual((task["tries"], task["prelaunch_failures"], task["generation"]), (0, 0, 0))
        self.assertEqual(task["receipt_refs"], [])
        self.assertEqual(self.native.calls, [])

    def test_real_guard_race_skip_is_not_a_prelaunch_failure(self):
        plan = self.plan()
        pilot = self.root / "race-public-original.json"
        atomic_json(pilot, {"requested_url": public_search_url("blinkit", "food paper", LOCATION),
            "native": {"navigation_started": True, "navigation_status": 429, "completed_at": NOW,
                       "owned_process_exited": True, "retry_after": "7200"}})
        def guard_arrives(callback, context, timeout):
            note_route_failure(self.memory.root, "blinkit", pilot)
            return callback(context)
        result = self.run_at(self.handlers(), runner=guard_arrives)
        self.assertEqual(result["handler_calls"], 1)
        task = self.task(plan)
        self.assertEqual((task["tries"], task["prelaunch_failures"]), (0, 0))
        self.assertEqual(timestamp(task["next_retry_at"]) - timestamp(NOW), timedelta(hours=2))
        self.assertEqual(self.native.calls, [])

    def test_due_actual_probe_runs_once_and_healthy_other_platform_does_not_wait(self):
        native = Native(status=429, retry_after="7200")
        first = self.plan()
        self.plan("different wording")
        other = self.plan("drain strainer", "zepto")
        handlers = self.handlers(native=native)
        self.assertEqual(self.run_at(handlers)["handler_calls"], 2)
        self.assertEqual(len(native.calls), 2)
        healthy = Native(status=200)
        due = (timestamp(NOW) + timedelta(hours=2)).isoformat()
        result = self.run_at(self.handlers(native=healthy), due)
        # Each blocked platform first proves recovery; its queued questions
        # may then continue in the same pass after that proof is applied.
        self.assertEqual(len(healthy.calls), 3)
        blinkit_tries = sorted(row["tries"] for row in self.memory.snapshot()["query_tasks"].values()
                               if row.get("route") == "blinkit")
        self.assertEqual(blinkit_tries, [1, 2])
        self.assertEqual(self.task(other)["tries"], 2)

    def test_saved_native_original_is_applied_before_wait_without_callback_slot(self):
        plan = self.plan(); handlers = self.handlers()
        def interrupted(callback, context, timeout):
            self.native(public_search_url("blinkit", self.request["query"], LOCATION), "fixture",
                        self.memory.root / "quickcommerce_free/acquisitions" / context["acquisition_id"] / "native")
            raise RuntimeError("Synthetic interruption before wrapper import")
        self.run_at(handlers, runner=interrupted)
        task = self.task(plan); original = task["acquisition_id"]
        old = task
        self.memory.put("query_tasks", plan["task_ids"][0], dict(task, next_retry_at="2026-10-10T10:00:00+00:00"), previous=old)
        policy = dict(DEFAULT_POLICY, source_callbacks=1, exploration_slots=0)
        turn = open_work_turn(self.memory, "programme", at=NOW, policy=policy)
        with bind_work_turn(self.memory, turn["id"]):
            result = self.run_at(handlers)
            self.assertEqual(active_work_turn(self.memory)["counters"]["source_callbacks"], 0)
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(self.task(plan)["acquisition_id"], original)
        self.assertEqual(self.task(plan)["tries"], 1)
        self.assertEqual(len(self.native.calls), 1)

    def test_new_funded_connection_ignores_only_proved_old_public_wait(self):
        plan = self.plan()
        legacy = create_quickcommerce_handlers(self.memory, native_capture=self.native)
        self.run_at(legacy)
        client, opener = self.client()
        result = self.run_at(self.handlers(client), "2026-10-09T10:00:30+00:00")
        self.assertEqual(result["handler_calls"], 1)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(len(self.native.calls), 1)
        self.assertEqual(self.task(plan)["status"], "acquired")

    def test_funded_provider_failure_cannot_be_waived_as_a_public_pause(self):
        plan = self.plan()
        opener = Opener(error=HTTPError("https://api.quickcommerceapi.com/v1/search", 429,
                                       "Synthetic provider limit", {}, io.BytesIO(b"")))
        client = QuickCommerceClient(SECRET, opener=opener)
        handlers = self.handlers(client)
        self.run_at(handlers)
        result = self.run_at(handlers, "2026-10-09T10:00:30+00:00")
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(self.task(plan)["tries"], 1)

    def test_hint_is_pure_and_retains_real_wait_instead_of_short_outer_default(self):
        plan = self.plan(); handlers = self.handlers(native=Native(status=429, retry_after="7200"))
        self.run_at(handlers)
        state = self.memory.snapshot(); task = self.task(plan)
        run = state["batch_runs"][key("source-run", "public-wait")]
        context = source_access_context(state, task, as_of=NOW, pause=run["route_pauses"]["blinkit"])
        hint = route_registry(handlers)["blinkit"]["source_access_hint"](context)
        self.assertFalse(hint["ready"])
        self.assertEqual(hint["next_retry_at"], task["next_retry_at"])
        self.assertEqual(state, self.memory.snapshot())

    def test_uncertain_due_recovery_holds_original_instead_of_another_keyword_probe(self):
        plan = self.plan()
        self.run_at(self.handlers(native=Native(status=429, retry_after="7200")))
        self.plan("another drain product")
        invocations = []
        def uncertain(url, expression, folder):
            invocations.append(url)
            raise RuntimeError("Synthetic process outcome lost")
        due = "2026-10-09T12:00:00+00:00"
        handlers = self.handlers(native=uncertain)
        self.run_at(handlers, due)
        self.assertEqual(len(invocations), 1)
        health = load_json(self.memory.root / "quickcommerce_free/route_guards/blinkit/health.json")
        self.assertEqual(health["recovery_probe"]["acquisition_id"], self.task(plan)["acquisition_id"])
        result = self.run_at(handlers, "2026-10-09T12:05:00+00:00")
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(len(invocations), 1)
        other = next(task for task in self.memory.snapshot()["query_tasks"].values()
                     if task.get("request", {}).get("query") == "another drain product")
        hint = route_registry(handlers)["blinkit"]["source_access_hint"](
            source_access_context(self.memory.snapshot(), other, as_of=due))
        self.assertFalse(hint["ready"])
        self.assertIsNone(hint["next_retry_at"])

    def test_new_funded_route_still_needs_callback_admission(self):
        plan = self.plan(); self.run_at(self.handlers())
        client, opener = self.client()
        policy = dict(DEFAULT_POLICY, source_callbacks=1, exploration_slots=0)
        turn = open_work_turn(self.memory, "programme", at=NOW, policy=policy)
        with bind_work_turn(self.memory, turn["id"]), \
                patch("product_radar.source_coverage.reserve_source_callback", side_effect=SafetyError("WORK_TURN_SOURCE_LIMIT")):
            result = self.run_at(self.handlers(client), "2026-10-09T10:00:30+00:00")
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(opener.calls, [])
        self.assertEqual(self.task(plan)["generation"], 1)

    def test_late_original_funded_response_applies_during_public_wait_without_new_credit(self):
        class Lost:
            calls = 0
            def search(self, request, *, on_dispatch=None):
                self.calls += 1
                if on_dispatch:
                    on_dispatch()
                raise RuntimeError("Synthetic original funded response is delayed")
        client = Lost(); plan = self.plan(); handlers = self.handlers(client)
        self.run_at(handlers)
        task = self.task(plan); acquisition = task["acquisition_id"]
        # The same original saved result arrives later, not a new search.
        healthy, opener = self.client()
        with patch("product_radar.quickcommerce.utc_now", return_value=NOW):
            response = healthy.search(self.request)
        path = self.memory.root / "quickcommerce_free/acquisitions" / acquisition / "funded_response.json"
        atomic_json(path, response)
        result = self.run_at(handlers, "2026-10-09T10:00:30+00:00")
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(client.calls, 1)
        self.assertEqual(self.task(plan)["status"], "acquired")
        self.assertEqual(self.task(plan)["acquisition_id"], acquisition)

    def test_saved_original_parse_does_not_depend_on_an_unrelated_damaged_guard(self):
        plan = self.plan(); handlers = self.handlers()
        def interrupted(callback, context, timeout):
            callback(context)
            raise RuntimeError("Synthetic lost local wrapper return")
        self.run_at(handlers, runner=interrupted)
        task = self.task(plan)
        projection = self.memory.root / "quickcommerce_free/acquisitions" / task["acquisition_id"] / "access_wait.json"
        # Keep real original probe but simulate interruption before projection
        # was saved. A different guard file cannot erase that original read.
        projection.unlink()
        atomic_json(self.memory.root / "quickcommerce_free/route_guards/blinkit/health.json", {"damaged": True})
        result = self.run_at(handlers)
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(self.task(plan)["tries"], 1)
        self.assertEqual(len(self.native.calls), 1)

    def test_joined_programme_readiness_observes_actual_wait_and_distinct_funded_connection(self):
        self.plan(); handlers = self.handlers(native=Native(status=429, retry_after="7200"))
        self.run_at(handlers)
        record = {"programme_id": "programme", "source_run_id": "public-wait", "concept_ids": ["paper"]}
        at = "2026-10-09T10:05:00+00:00"
        readiness = _source_turn_readiness(self.memory, self.memory.snapshot(), record, handlers, at)
        self.assertFalse(readiness["callback_ready"])
        self.assertIn("2026-10-09T12:00:00+00:00", readiness["waits"])
        client, opener = self.client()
        funded = _source_turn_readiness(self.memory, self.memory.snapshot(), record, self.handlers(client), at)
        self.assertTrue(funded["callback_ready"])
        self.assertEqual(opener.calls, [])

    def test_joined_programme_readiness_does_not_waive_a_real_funded_failure(self):
        self.plan()
        opener = Opener(error=HTTPError("https://api.quickcommerceapi.com/v1/search", 429,
                                       "Synthetic provider limit", {}, io.BytesIO(b"")))
        handlers = self.handlers(QuickCommerceClient(SECRET, opener=opener))
        self.run_at(handlers)
        record = {"programme_id": "programme", "source_run_id": "public-wait", "concept_ids": ["paper"]}
        readiness = _source_turn_readiness(self.memory, self.memory.snapshot(), record, handlers,
                                          "2026-10-09T10:00:30+00:00")
        self.assertFalse(readiness["callback_ready"])
        self.assertEqual(len(opener.calls), 1)


if __name__ == "__main__":
    unittest.main()
