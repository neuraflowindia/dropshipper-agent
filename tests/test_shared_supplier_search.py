"""Genuine installed supplier leaves with synthetic receipts, never live IO.

TradeIndia stands for the plain-read-first routes (a plain read, then a
browser read only after proof); Google Maps and IndiaMART start in their own
browser read (see test_supplier_google_routes and test_indiamart_signed_in).
"""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar.catalogue_discovery import advance_discovery_cycle, prepare_next_discovery_wave
from product_radar.functional_types import effective_functional_definition
from product_radar.models import SafetyError, digest
from product_radar.source_acquisitions import ACQUISITION_KIND, claim_source_acquisition
from product_radar.source_deadline import SourceActionDeadline
from product_radar.source_coverage import execute_source_run, prepare_source_tasks, product_source_request_builder
from product_radar.storage import atomic_json, load_json
from product_radar.supplier_route_acquisition import (ACTIVE_SEARCH_ROUTES, create_supplier_handlers, SEARCH_ROUTES,
    supplier_route_url)
from product_radar.supplier_search import (APPLICATION_KIND, current_supplier_search_checks, prepare_supplier_request,
    supplier_request_for_state, validate_supplier_lead_receipt)
from product_radar.work_turns import bind_work_turn, close_work_turn, open_work_turn
import test_functional_queries as functional_fixture
from test_functional_queries import Runner, NOW
from test_supplier_route_acquisition import BODIES, receipt


class SharedSupplierTests(unittest.TestCase):
    def setUp(self):
        fixture = functional_fixture.FunctionalQueryTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.accept(); fixture.prepare(Runner())
        self.memory, self.cid = fixture.memory, fixture.cid
        self.fixture = fixture
        self.catalogue = deepcopy(fixture.catalogue)
        self.query = "draining sink caddy"; self.at = NOW
        self.catalogue["families"][0]["phrases"] = [{**self.catalogue["families"][0]["phrases"][0],
            "text": self.query, "source": "supplier", "purpose": "discovery"}]
        self.http_calls, self.browser_calls = [], []; self.mode = "usable"
        self.before = self.memory.snapshot()
        # The signed-in browser's records (sign-ins, pacing) for this test only.
        signed = tempfile.TemporaryDirectory(prefix="radar-signed-in-"); self.addCleanup(signed.cleanup)
        self.signed_root = Path(signed.name)

    def reader(self, item):
        self.http_calls.append(deepcopy(item))
        if self.mode == "unknown": raise TimeoutError("Synthetic owned read interrupted")
        route = next(route for route in SEARCH_ROUTES if supplier_route_url(route, self.query) == item["url"])
        body = BODIES[route].replace("Food wrap paper", "Draining sink caddy")
        if self.mode == "layout": body = "<html><body>Readable different layout</body></html>"
        if self.mode == "empty": body = "<html><body>No products found</body></html>"
        result = receipt(item, body=body, status=429 if self.mode == "blocked" else 200)
        if self.mode == "server-error": result = receipt(item, body=body, status=503)
        result.update(checked_at=self.at, attempt_started_at=self.at,
                      transport_contract="public-page-owned-http-v1", owned_worker_exited=True)
        if self.mode in {"blocked", "server-error"}: result["retry_after"] = "3600"
        return result

    def browser(self, item):
        self.browser_calls.append(deepcopy(item))
        route = next(route for route in SEARCH_ROUTES if supplier_route_url(route, self.query) == item["url"])
        body = BODIES[route].replace("Food wrap paper", "Draining sink caddy")
        if self.mode == "empty": body = "<html><body>No products found</body></html>"
        return dict(receipt(item, body=body), checked_at=self.at, attempt_started_at=self.at,
                    execution_ref="supplier-browser-" + "b" * 32, owned_process_exited=True)

    def handlers(self, *, routes=("tradeindia",)):
        handlers = create_supplier_handlers(self.memory, http_reader=self.reader, browser_reader=self.browser,
            respect_access_waits=True, shared_acquisitions=True, signed_in_root=self.signed_root)
        return {route: handlers[route] for route in routes}

    def prepare(self, handlers, *, route="tradeindia", run="supplier-checks", request=None):
        self.run = run
        request = request or {"query": self.query, "country": "IN"}
        prepare_source_tasks(self.memory, [{"route": route, "request": request, "concept_ids": [self.cid]}],
                             run_id=run, as_of=self.at)
        self.wave = prepare_next_discovery_wave(self.memory, "supplier-programme", self.catalogue,
            handlers=handlers, include_lists=False, as_of=self.at)

    def signed_in_page(self, item, *, folder, root):
        """The signed-in IndiaMART read's receipt; test_indiamart_signed_in drives the real one."""
        self.assertEqual(root, self.signed_root)
        return dict(self.browser(item), profile="signed-in")

    def sign_in(self):
        from product_radar.signed_in_browser import record_site_observation
        record_site_observation(self.signed_root, "indiamart", True, observed_at=self.at)

    def checks(self, handlers):
        with patch("product_radar.supplier_search.utc_now", return_value=self.at), \
                patch("product_radar.indiamart_signed_in.fetch_indiamart_signed_in_page", side_effect=self.signed_in_page):
            return execute_source_run(self.memory, self.run, handlers=handlers, as_of=self.at,
                shared_marketplace=True, bounded_call=lambda callback, context, timeout: callback(context))

    def discovery(self, handlers):
        turn = open_work_turn(self.memory, self.wave["programme_id"], at=self.at)
        try:
            with bind_work_turn(self.memory, turn["id"]), patch("product_radar.supplier_search.utc_now", return_value=self.at):
                return advance_discovery_cycle(self.memory, self.wave["id"], handlers=handlers, as_of=self.at,
                    shared_marketplace=True, max_actions=4, admission=lambda *_args, **_kwargs: None)
        finally:
            close_work_turn(self.memory, turn["id"], at=self.at, reason="Synthetic bounded supplier search")

    def owners(self):
        return {rid: row for rid, row in self.memory.snapshot()["batch_runs"].items() if row.get("kind") == ACQUISITION_KIND}

    def test_direction_capture_then_real_type_check_reuses_one_original(self):
        handlers = self.handlers(); self.prepare(handlers)
        self.assertEqual(self.discovery(handlers)["external_handler_calls_this_round"], 1)
        self.assertEqual(self.checks(handlers)["handler_calls"], 0)
        self.assertEqual(len(self.http_calls), 1); self.assertEqual(len(self.owners()), 1)
        state = self.memory.snapshot()
        self.assertEqual(state["observations"], self.before["observations"])
        self.assertEqual(state["concepts"], self.before["concepts"])
        self.assertEqual(state["market_reviews"], self.before["market_reviews"])
        apps = [row for row in state["source_checks"].values() if row.get("kind") == APPLICATION_KIND]
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]["binding"]["meaning_hash"], effective_functional_definition(state, self.cid)["meaning_hash"])
        checks = current_supplier_search_checks(state, self.cid, description_hash=digest(state["concepts"][self.cid]["description"]))
        self.assertEqual(len(checks), 1)
        self.assertTrue(next(iter(checks.values()))["candidates"])
        self.assertTrue(all(q["purpose"] == "sourcing" for q in state["query_tasks"].values()
                            if q.get("kind") == "research-question-v2"))
        action = next(iter(state["batch_runs"][self.wave["id"]]["actions"].values()))
        self.assertEqual(action["state"], "acquired")
        self.assertIn("supplier_lead_capture_ref", action)

    def test_check_first_then_direction_keeps_date_and_zero_more_reads(self):
        handlers = self.handlers(); self.prepare(handlers)
        self.assertEqual(self.checks(handlers)["counts"], {"acquired": 1})
        self.assertEqual(self.discovery(handlers)["external_handler_calls_this_round"], 0)
        self.assertEqual(len(self.http_calls), 1)
        owner = next(iter(self.owners().values()))
        response = self.memory.snapshot()["source_checks"][owner["active_response_id"]]
        self.assertEqual(response["observed_at"], NOW)
        self.assertEqual(response["expense_proof"]["provider_requests"], 0)

    def test_completed_supplier_direction_waits_until_actual_original_expiry(self):
        handlers = self.handlers(); self.prepare(handlers); self.discovery(handlers)
        later = "2026-10-06T10:00:00+00:00"
        waiting = prepare_next_discovery_wave(self.memory, "supplier-programme", self.catalogue,
            handlers=handlers, include_lists=False, as_of=later)
        self.assertEqual(waiting["state"], "waiting")
        self.assertEqual(waiting["next_retry_at"], "2026-10-12T10:00:00+00:00")
        self.assertEqual(len(self.http_calls), 1); self.assertEqual(len(self.owners()), 1)

    def test_all_active_supplier_routes_capture_without_new_products(self):
        # Installed for all four, but the retired Google Search route is never planned or sent.
        # Each active route starts with its own read: TradeIndia plainly, Google Maps
        # in the browser; IndiaMART waits calmly because the owner has not signed in.
        handlers = self.handlers(routes=SEARCH_ROUTES); self.prepare(handlers)
        self.assertEqual(self.discovery(handlers)["external_handler_calls_this_round"], 2)
        self.assertEqual([item["url"] for item in self.http_calls], [supplier_route_url("tradeindia", self.query)])
        self.assertEqual([item["url"] for item in self.browser_calls], [supplier_route_url("google-maps", self.query)])
        planned = [question for planned in self.memory.snapshot()["batch_runs"][self.wave["id"]]["planned_actions"]
                   for question in planned["questions"]]
        self.assertEqual({question["route"]: question["request"].get("transport") for question in planned},
                         {"tradeindia": "public-http", "google-maps": "public-browser", "indiamart": "signed-in-browser"})
        self.assertEqual(set(ACTIVE_SEARCH_ROUTES), {"tradeindia", "google-maps", "indiamart"})
        self.assertFalse(any(question["route"] == "google-suppliers" for planned in
                             self.memory.snapshot()["batch_runs"][self.wave["id"]]["planned_actions"]
                             for question in planned["questions"]))
        self.assertEqual(self.memory.snapshot()["concepts"], self.before["concepts"])

    def test_browser_fallback_is_separate_owner_after_closed_layout_failure(self):
        self.mode = "layout"; handlers = self.handlers(); self.prepare(handlers)
        self.assertEqual(self.checks(handlers)["counts"], {"insufficient": 1})
        request = supplier_request_for_state(self.memory.snapshot(), "tradeindia", self.query)
        self.assertEqual(request["transport"], "public-browser")
        self.prepare(handlers, run="browser-fallback", request=request)
        self.assertEqual(self.checks(handlers)["counts"], {"acquired": 1})
        self.assertEqual(len(self.http_calls), 1); self.assertEqual(len(self.browser_calls), 1)
        self.assertEqual(len(self.owners()), 2)
        self.assertEqual({o["binding"]["request"]["transport"] for o in self.owners().values()}, {"public-http", "public-browser"})

    def test_queued_browser_read_without_proof_waits_and_reads_nothing(self):
        handlers = self.handlers(); self.prepare(handlers)
        self.assertEqual(self.checks(handlers)["counts"], {"acquired": 1})
        request = dict(supplier_request_for_state(self.memory.snapshot(), "tradeindia", self.query), transport="public-browser")
        self.prepare(handlers, run="unproved-browser", request=request)
        result = self.checks(handlers)
        self.assertEqual((result["handler_calls"], result["results"][0]["reason"]),
                         (0, "SUPPLIER_BROWSER_FALLBACK_PROOF_REQUIRED"))
        self.assertEqual((len(self.http_calls), self.browser_calls), (1, []))

    def test_true_empty_does_not_request_browser_or_claim_no_demand(self):
        self.mode = "empty"; handlers = self.handlers(); self.prepare(handlers)
        self.assertEqual(self.checks(handlers)["counts"], {"completed_no_results": 1})
        self.assertEqual(supplier_request_for_state(self.memory.snapshot(), "tradeindia", self.query)["transport"], "public-http")
        self.assertEqual(self.browser_calls, [])
        self.assertEqual(self.memory.snapshot()["market_reviews"], self.before["market_reviews"])

    def test_uncertain_original_never_launches_second_read(self):
        self.mode = "unknown"; handlers = self.handlers(); self.prepare(handlers)
        self.checks(handlers); self.checks(handlers); self.discovery(handlers)
        self.assertEqual(len(self.http_calls), 1); self.assertEqual(self.browser_calls, [])
        self.assertEqual(len(self.owners()), 1)

    def test_parser_failure_replays_saved_bytes_without_second_http_request(self):
        handlers = self.handlers(); self.prepare(handlers)
        with patch("product_radar.supplier_search.parse_supplier_route_receipt", side_effect=ValueError("Synthetic parser fault")):
            self.checks(handlers)
        self.assertEqual(len(self.http_calls), 1)
        self.assertEqual(self.checks(handlers)["handler_calls"], 0)
        self.assertEqual(len(self.http_calls), 1)
        self.assertTrue(next(iter(self.owners().values()))["active_response_id"])

    def test_saved_lead_reference_cannot_be_forged(self):
        handlers = self.handlers(); self.prepare(handlers); self.discovery(handlers)
        action = next(iter(self.memory.snapshot()["batch_runs"][self.wave["id"]]["actions"].values()))
        saved = load_json(Path(action["reference"]))
        changed = deepcopy(saved); changed["supplier_lead_capture_ref"]["hash"] = "0" * 64
        with self.assertRaisesRegex(SafetyError, "SUPPLIER_SHARED_LEAD_PROOF_INVALID"):
            validate_supplier_lead_receipt(self.memory, prepare_supplier_request({"route": "tradeindia",
                "request": {"query": self.query, "country": "IN"}}), action["acquisition_id"], changed)

    def test_unclosed_http_keeps_leads_but_cannot_refresh_or_authorise_browser(self):
        original = self.reader
        def unclosed(item): return dict(original(item), owned_worker_exited=False)
        handlers = create_supplier_handlers(self.memory, http_reader=unclosed, browser_reader=self.browser,
            shared_acquisitions=True, respect_access_waits=True)
        handlers = {"tradeindia": handlers["tradeindia"]}; self.prepare(handlers); self.checks(handlers)
        aid, owner = next(iter(self.owners().items()))
        response = self.memory.snapshot()["source_checks"][owner["active_response_id"]]
        self.assertEqual(response["source_state"], "partial"); self.assertFalse(response["complete"])
        self.assertTrue(current_supplier_search_checks(self.memory.snapshot(), self.cid,
            description_hash=digest(self.before["concepts"][self.cid]["description"])))
        self.assertEqual(supplier_request_for_state(self.memory.snapshot(), "tradeindia", self.query)["transport"], "public-http")
        later = "2026-10-13T10:00:00+00:00"
        result = claim_source_acquisition(self.memory, "tradeindia", owner["binding"]["request"],
            owner_id="later-caller", as_of=later, artifact_roots=owner["binding"]["artifact_roots"],
            refresh_due_at=later, refresh_reason="Useful due supplier refresh")
        self.assertEqual(result["acquisition_id"], aid); self.assertEqual(len(self.owners()), 1)
        self.assertEqual(self.browser_calls, [])

    def _legacy(self, *, mode="usable"):
        self.mode = mode; self.run = "original-legacy"
        prepare_source_tasks(self.memory, [{"route": "tradeindia", "request": {"query": self.query, "country": "IN"},
            "concept_ids": [self.cid]}], run_id=self.run, as_of=self.at)
        handlers = create_supplier_handlers(self.memory, http_reader=self.reader, browser_reader=self.browser)
        with patch("product_radar.supplier_route_acquisition.utc_now", return_value=self.at):
            execute_source_run(self.memory, self.run, handlers=handlers, as_of=self.at,
                bounded_call=lambda callback, context, timeout: callback(context))
        task = next(row for row in self.memory.snapshot()["query_tasks"].values()
                    if row.get("kind") == "all_source_task_v1" and row.get("run_id") == self.run)
        self.mode = "usable"
        return task

    def test_genuine_legacy_single_read_keeps_original_bytes_id_date_and_no_repeat(self):
        original_task = self._legacy()
        path = self.memory.root / "supplier_routes" / original_task["acquisition_id"] / "capture.json"
        before = path.read_bytes(); handlers = self.handlers(); self.prepare(handlers, run="new-check")
        self.assertEqual(self.checks(handlers)["handler_calls"], 0)
        self.assertEqual(len(self.http_calls), 1); self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.owners()), [original_task["acquisition_id"]])
        owner = next(iter(self.owners().values()))
        response = self.memory.snapshot()["source_checks"][owner["active_response_id"]]
        self.assertEqual(response["observed_at"], self.at)
        self.assertEqual(response["legacy_workflow_id"], original_task["acquisition_id"])
        self.assertEqual(response["expense_proof"]["collection_channel"], "unknown")

    def test_genuine_legacy_two_transport_bundle_reuses_both_without_another_read(self):
        original_task = self._legacy(mode="layout")
        handlers = self.handlers(); self.prepare(handlers, run="legacy-http-join"); self.checks(handlers)
        self.assertEqual(len(self.owners()), 2)
        request = supplier_request_for_state(self.memory.snapshot(), "tradeindia", self.query)
        self.assertEqual(request["transport"], "public-browser")
        self.prepare(handlers, run="legacy-browser-join", request=request)
        self.assertEqual(self.checks(handlers)["handler_calls"], 0)
        self.assertEqual(len(self.http_calls), 1); self.assertEqual(len(self.browser_calls), 1)
        self.assertTrue(current_supplier_search_checks(self.memory.snapshot(), self.cid,
            description_hash=digest(self.before["concepts"][self.cid]["description"])))
        self.assertIn(original_task["acquisition_id"], self.owners())
        browser_owner = next(row for row in self.owners().values() if row["binding"]["request"]["transport"] == "public-browser")
        self.assertEqual(browser_owner["binding"]["original_attempt_id"], self.browser_calls[0]["id"])

    def test_current_company_followup_uses_actual_saved_urls_only(self):
        handlers = self.handlers(); self.prepare(handlers); self.checks(handlers)
        state = self.memory.snapshot(); description_hash = digest(state["concepts"][self.cid]["description"])
        builder = product_source_request_builder(shared_suppliers=True)
        request = builder({"state": state, "concept_id": self.cid, "description_hash": description_hash, "route": "company-pages"})
        checks = current_supplier_search_checks(state, self.cid, description_hash=description_hash)
        urls = sorted({row["url"] for check in checks.values() for row in check["candidates"]})
        self.assertEqual(request["candidate_urls"], {self.cid: urls})
        self.assertEqual(request["candidate_check_ids"], {self.cid: sorted(checks)})

    def test_shared_429_wait_retains_deadline_and_healthy_other_host(self):
        self.mode = "blocked"; handlers = self.handlers(routes=("google-maps", "tradeindia"))
        self.prepare(handlers, route="tradeindia"); self.checks(handlers)
        task = next(row for row in self.memory.snapshot()["query_tasks"].values()
            if row.get("kind") == "all_source_task_v1" and row.get("run_id") == self.run)
        self.assertEqual(task["next_retry_at"], "2026-10-05T11:00:00+00:00")
        self.mode = "usable"; self.prepare(handlers, route="tradeindia", run="same-host")
        self.assertEqual(self.checks(handlers)["handler_calls"], 0)
        self.prepare(handlers, route="google-maps", run="healthy-host")
        self.assertEqual(self.checks(handlers)["handler_calls"], 1)
        self.assertEqual((len(self.http_calls), len(self.browser_calls)), (1, 1))

    def test_current_meaning_change_does_not_inherit_old_supplier_nominations(self):
        handlers = self.handlers(); self.prepare(handlers); self.checks(handlers)
        definition = deepcopy(functional_fixture.DEFINITION)
        definition["included_distinctions"] = ["Draining base", "Corner mounting"]
        self.fixture.accept(definition, at="2026-10-06T10:00:00+00:00")
        state = self.memory.snapshot()
        self.assertEqual(current_supplier_search_checks(state, self.cid,
            description_hash=digest(state["concepts"][self.cid]["description"])), {})
        with self.assertRaisesRegex(SafetyError, "SOURCE_SUPPLIER_DESTINATIONS_PENDING"):
            product_source_request_builder(shared_suppliers=True)({"state": state, "concept_id": self.cid,
                "description_hash": digest(state["concepts"][self.cid]["description"]), "route": "company-pages"})

    def test_legacy_unknown_retains_id_and_waits_for_original_without_new_read(self):
        run = "legacy-unknown"
        prepare_source_tasks(self.memory, [{"route": "tradeindia", "request": {"query": self.query, "country": "IN"},
            "concept_ids": [self.cid]}], run_id=run, as_of=self.at)
        old_handlers = create_supplier_handlers(self.memory, http_reader=self.reader)
        def interrupted(*_args): raise TimeoutError("Synthetic wrapper lost before reply")
        execute_source_run(self.memory, run, handlers=old_handlers, as_of=self.at, bounded_call=interrupted)
        old = next(row for row in self.memory.snapshot()["query_tasks"].values()
                   if row.get("kind") == "all_source_task_v1" and row.get("run_id") == run)
        handlers = self.handlers(); self.prepare(handlers, run="new-unknown-join")
        self.assertEqual(self.checks(handlers)["handler_calls"], 0)
        self.assertEqual(self.http_calls, []); self.assertEqual(self.browser_calls, [])
        self.assertEqual(list(self.owners()), [old["acquisition_id"]])
        self.assertEqual(next(iter(self.owners().values()))["state"], "execution_unknown")
        # Arrival of that exact old original, not a new shared read, wakes
        # saved-only reconciliation and preserves its old source date.
        from product_radar.supplier_route_acquisition import acquire_supplier_search
        acquire_supplier_search(self.memory.root, route="tradeindia", query=self.query,
            acquisition_id=old["acquisition_id"], http_reader=self.reader, as_of=self.at)
        before = len(self.http_calls)
        self.assertEqual(self.checks(handlers)["handler_calls"], 0)
        self.assertEqual(len(self.http_calls), before)
        self.assertTrue(next(iter(self.owners().values()))["active_response_id"])

    def test_late_original_closed_browser_files_replay_without_another_callback(self):
        self.mode = "layout"; handlers = self.handlers(); self.prepare(handlers); self.checks(handlers)
        request = supplier_request_for_state(self.memory.snapshot(), "tradeindia", self.query)
        handlers = create_supplier_handlers(self.memory, http_reader=self.reader,
            respect_access_waits=True, shared_acquisitions=True)
        handlers = {"tradeindia": handlers["tradeindia"]}
        self.prepare(handlers, run="late-browser", request=request)
        native_calls = []
        def completed_then_interrupted(item, *, route, folder):
            native_calls.append(deepcopy(item)); folder.mkdir(parents=True, exist_ok=True)
            completed = "2026-10-05T10:00:02+00:00"
            output = {"state": "captured", "navigation_started": True, "navigation_started_at": NOW,
                "navigation_status": 200, "completed_at": completed, "owned_process_exited": True,
                "snapshot": {"url": item["url"], "html": BODIES["tradeindia"], "text": "Draining sink caddy",
                             "title": "Synthetic directory", "links": []}}
            atomic_json(folder / "native_result.json", output)
            atomic_json(folder / "native_dispatch.json", {"url": item["url"], "navigation_started": True,
                                                           "navigation_started_at": NOW})
            raise TimeoutError("Synthetic wrapper interrupted after owned child completed")
        with patch("product_radar.supplier_search.fetch_supplier_native_page", side_effect=completed_then_interrupted):
            self.checks(handlers)
        self.assertEqual(len(native_calls), 1)
        self.at = "2026-10-05T10:00:03+00:00"
        self.assertEqual(self.checks(handlers)["handler_calls"], 0)
        self.assertEqual(len(native_calls), 1); self.assertEqual(len(self.http_calls), 1)
        browser_owner = next(row for row in self.owners().values() if row["binding"]["request"]["transport"] == "public-browser")
        response = self.memory.snapshot()["source_checks"][browser_owner["active_response_id"]]
        self.assertEqual(response["observed_at"], "2026-10-05T10:00:02+00:00")
        self.assertTrue(response["complete"])

    def test_closed_server_failure_allows_one_due_new_caller_probe_not_browser_bypass(self):
        self.mode = "server-error"; handlers = self.handlers(); self.prepare(handlers); self.checks(handlers)
        original_id = next(iter(self.owners()))
        self.assertEqual(self.browser_calls, [])
        self.prepare(handlers, run="waiting-new-caller")
        self.assertEqual(self.checks(handlers)["handler_calls"], 0)
        self.at = "2026-10-05T11:00:00+00:00"; self.mode = "usable"
        self.prepare(handlers, run="due-new-caller")
        self.assertEqual(self.checks(handlers)["handler_calls"], 1)
        self.assertEqual(len(self.http_calls), 2); self.assertEqual(self.browser_calls, [])
        owners = self.owners(); self.assertEqual(len(owners), 2)
        renewed = next(row for aid, row in owners.items() if aid != original_id)
        self.assertEqual(renewed["binding"]["previous_acquisition_id"], original_id)
        self.prepare(handlers, run="joined-due-caller")
        self.assertEqual(self.checks(handlers)["handler_calls"], 0); self.assertEqual(len(self.http_calls), 2)

    def test_application_commit_rechecks_actual_original_bytes(self):
        handlers = self.handlers(); self.prepare(handlers)
        commit = self.memory.commit; changed = []
        def replace_before_application(event_id, changes, **options):
            if not changed and any(row.get("value", {}).get("kind") == APPLICATION_KIND for row in changes):
                owner = next(iter(self.owners().items()))
                path = self.memory.root / "supplier_routes" / "shared" / owner[0] / "page.json"
                data = load_json(path); data["receipt"]["text"] += " Changed after acquisition proof."
                data["hash"] = digest(data["receipt"]); atomic_json(path, data); changed.append(path)
            return commit(event_id, changes, **options)
        with patch.object(self.memory, "commit", side_effect=replace_before_application):
            self.checks(handlers)
        self.assertTrue(changed)
        self.assertFalse(any(row.get("kind") == APPLICATION_KIND for row in self.memory.snapshot()["source_checks"].values()))
        self.assertEqual(len(self.http_calls), 1)

    def test_old_finished_searches_cannot_close_a_changed_types_company_search(self):
        self.mode = "empty"; handlers = self.handlers(routes=SEARCH_ROUTES); self.run = "all-old-supplier-checks"
        self.sign_in()
        prepare_source_tasks(self.memory, [{"route": route, "request": {"query": self.query, "country": "IN"},
            "concept_ids": [self.cid]} for route in SEARCH_ROUTES], run_id=self.run, as_of=self.at)
        self.checks(handlers)
        state = self.memory.snapshot(); description_hash = digest(state["concepts"][self.cid]["description"])
        builder = product_source_request_builder(shared_suppliers=True)
        self.assertIn("not_applicable_reason", builder({"state": state, "concept_id": self.cid,
            "description_hash": description_hash, "route": "company-pages"}))
        definition = deepcopy(functional_fixture.DEFINITION); definition["included_distinctions"].append("Corner mounting")
        self.fixture.accept(definition, at="2026-10-06T10:00:00+00:00")
        with self.assertRaisesRegex(SafetyError, "SOURCE_SUPPLIER_DESTINATIONS_PENDING"):
            builder({"state": self.memory.snapshot(), "concept_id": self.cid,
                "description_hash": description_hash, "route": "company-pages"})

    def test_late_native_recovery_keeps_earlier_actual_date_and_clears_pending_original(self):
        self.mode = "layout"; handlers = self.handlers(); self.prepare(handlers); self.checks(handlers)
        request = supplier_request_for_state(self.memory.snapshot(), "tradeindia", self.query)
        def interrupted(item):
            error = SourceActionDeadline()
            error.execution_receipt = {"schema_version": 2, "id": item["id"], "requested_url": item["url"],
                "final_url": None, "checked_at": "2026-10-05T10:00:05+00:00", "http_status": None,
                "execution_stage": "attempt_started", "attempt_started_at": NOW,
                "execution_ref": "supplier-browser-" + "b" * 32, "body_complete": False}
            raise error
        handlers = create_supplier_handlers(self.memory, http_reader=self.reader, browser_reader=interrupted,
            shared_acquisitions=True, respect_access_waits=True)
        handlers = {"tradeindia": handlers["tradeindia"]}; self.prepare(handlers, run="late-closed-original", request=request)
        self.checks(handlers); self.at = "2026-10-05T10:00:06+00:00"; self.checks(handlers)
        task = next(row for row in self.memory.snapshot()["query_tasks"].values()
                    if row.get("kind") == "all_source_task_v1" and row.get("run_id") == self.run)
        self.assertTrue(task["supplier_original_pending"]); self.assertTrue(task["reconcile_required"])
        aid = task["acquisition_id"]; folder = self.memory.root / "supplier_routes" / "shared" / aid / "native"
        atomic_json(folder / "native_result.json", {"state": "captured", "navigation_started": True,
            "navigation_started_at": NOW, "navigation_status": 200, "owned_process_exited": True,
            "completed_at": "2026-10-05T10:00:02+00:00", "snapshot": {"url": request["url"],
                "html": BODIES["tradeindia"], "text": "Draining sink caddy", "title": "Synthetic directory", "links": []}})
        atomic_json(folder / "native_dispatch.json", {"url": request["url"], "navigation_started": True,
                                                       "navigation_started_at": NOW})
        self.at = "2026-10-05T10:00:07+00:00"
        self.assertEqual(self.checks(handlers)["handler_calls"], 0)
        task = next(row for row in self.memory.snapshot()["query_tasks"].values()
                    if row.get("kind") == "all_source_task_v1" and row.get("run_id") == self.run)
        self.assertFalse(task["supplier_original_pending"]); self.assertFalse(task["reconcile_required"])
        self.assertTrue(current_supplier_search_checks(self.memory.snapshot(), self.cid,
            description_hash=digest(self.before["concepts"][self.cid]["description"])))
        owner = self.owners()[aid]
        response = self.memory.snapshot()["source_checks"][owner["active_response_id"]]
        self.assertEqual(response["observed_at"], "2026-10-05T10:00:02+00:00")
        self.assertTrue(response["complete"]); self.assertEqual(len(self.http_calls), 1)

