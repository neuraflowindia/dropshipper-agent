"""Paid quick-commerce product checks: run-time admission and the credit gate (Plan R10 W5).

Synthetic provider replies only (searches and free balance checks); no real
key, network, purchase or paid search.
"""
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from product_radar.catalogue_discovery import discovery_wave_readiness
from product_radar.models import SafetyError
from product_radar.programme import _source_turn_readiness
from product_radar.quickcommerce import CREDITS_ENDPOINT, PLATFORMS, QuickCommerceClient, prepare_query
from product_radar.quickcommerce_handlers import create_quickcommerce_handlers
from product_radar.research_memory import ResearchMemory
from product_radar.source_acquisitions import ACQUISITION_KIND
from product_radar.source_coverage import execute_source_run, installed_source_handlers, prepare_source_tasks
from product_radar.spend_watch import load_state
from product_radar.spend_watch_quickcommerce import credit_wait
from product_radar.storage import Journal
from product_radar.work_turns import DEFAULT_POLICY, bind_work_turn, open_work_turn
from test_catalogue_expansion import fixture
from test_quickcommerce import Opener, Response, SECRET, credits_payload, payload
from test_supplier_discovery import CONCEPT

NOW = "2026-10-09T10:00:00+00:00"
LATER = "2026-10-09T10:06:00+00:00"          # past the 300 s question retry
AFTER_RECHECK = "2026-10-09T11:10:00+00:00"   # past the hour's credits / admission recheck
MUMBAI = {"name": "Mumbai", "lat": "19.0760", "lon": "72.8777"}


def digest_of(value):
    from product_radar.models import digest
    return digest(value)
DELHI = {"name": "Delhi NCR", "lat": "28.6139", "lon": "77.2090"}


class Provider:
    """Answers each search for exactly what was asked; counts searches and balance checks."""

    def __init__(self, *, available=44, remaining=43, status=200):
        self.available, self.remaining, self.status = available, remaining, status
        self.calls, self.credit_calls = [], []

    def open(self, request, timeout):
        if request.full_url == CREDITS_ENDPOINT:
            self.credit_calls.append(request.full_url)
            if isinstance(self.available, Exception):
                raise self.available
            return Response(json.dumps(credits_payload(self.available)).encode())
        self.calls.append(request.full_url)
        if self.status != 200:
            raise HTTPError(request.full_url, self.status, "Synthetic refusal", {}, io.BytesIO(b""))
        query = parse_qs(urlsplit(request.full_url).query)
        platform = next(name for name, label in PLATFORMS.items() if label == query["platform"][0])
        prepared = prepare_query(platform, query["q"][0], {"name": "Asked", "lat": query["lat"][0], "lon": query["lon"][0]})
        body = payload(prepared)
        body["credits_remaining"] = self.available = self.remaining  # the balance follows the reply
        return Response(json.dumps(body).encode())


class SharedQuickCommerceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); self.now = NOW
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.memory.put("concepts", "sink", deepcopy(CONCEPT))
        self.request = prepare_query("blinkit", "sink caddy", MUMBAI)
        self.opener = Opener(Response(json.dumps(payload(self.request)).encode()))
        self.client = QuickCommerceClient(SECRET, opener=self.opener)
        self.run = "checks"
        # Admission at run time is the real rule; its record reads are pinned here.
        admission = patch("product_radar.quickcommerce_plan.admission_hold", return_value=None)
        relevance = patch("product_radar.quickcommerce_plan.first_place_relevant", return_value=False)
        self.admission, self.relevance = admission.start(), relevance.start()
        self.addCleanup(admission.stop); self.addCleanup(relevance.stop)

    def handlers(self, client=None, routes=("blinkit",)):
        handlers = create_quickcommerce_handlers(self.memory, client or self.client, spend_root=self.root)
        return {route: handlers[route] for route in routes}

    def prepare(self, questions=None, place=MUMBAI):
        questions = questions or [("blinkit", "sink caddy")]
        prepare_source_tasks(self.memory, [{"route": route, "request": {"query": query, "location": deepcopy(place)},
            "concept_ids": ["sink"]} for route, query in questions], run_id=self.run, as_of=NOW)

    def checks(self, handlers, at=None):
        at = at or self.now
        with patch("product_radar.quickcommerce.utc_now", return_value=at), \
             patch("product_radar.quickcommerce_handlers.utc_now", return_value=at):
            return execute_source_run(self.memory, self.run, handlers=handlers, as_of=at, shared_marketplace=True,
                bounded_call=lambda callback, context, timeout: callback(context))

    def owners(self):
        return {rid: row for rid, row in self.memory.snapshot()["batch_runs"].items() if row.get("kind") == ACQUISITION_KIND}

    def tasks(self):
        return [task for task in self.memory.snapshot()["query_tasks"].values() if task.get("route") in PLATFORMS]

    # ----- the paid leaf ----------------------------------------------------

    def test_delayed_funded_completion_uses_actual_time_across_iso_offsets(self):
        self.now = "2026-10-09T15:30:00+05:30"
        completed, committed = "2026-10-09T10:00:03+00:00", "2026-10-09T10:00:05+00:00"
        handlers = self.handlers(); self.prepare()
        with patch("product_radar.quickcommerce.utc_now", return_value=completed), \
             patch("product_radar.quickcommerce_handlers.utc_now", return_value=committed):
            result = execute_source_run(self.memory, self.run, handlers=handlers, as_of=self.now,
                shared_marketplace=True, bounded_call=lambda callback, context, timeout: callback(context))
        self.assertEqual(result["counts"], {"acquired": 1})
        state = self.memory.snapshot(); owner = next(iter(self.owners().values()))
        response = state["source_checks"][owner["active_response_id"]]
        self.assertEqual(response["observed_at"], completed)
        self.assertEqual(response["recorded_at"], committed)
        self.assertEqual(len(self.opener.calls), 1)

    def test_check_binds_the_exact_paid_http_original(self):
        handlers = self.handlers(); self.prepare()
        self.assertEqual(self.checks(handlers)["counts"], {"acquired": 1})
        self.assertEqual(len(self.opener.calls), 1)
        owner = next(iter(self.owners().values())); state = self.memory.snapshot()
        self.assertEqual(owner["binding"]["request"]["transport"], "funded-http")
        self.assertEqual(owner["binding"]["request"]["location"], self.request["location"])
        response = state["source_checks"][owner["active_response_id"]]
        self.assertTrue(response["complete"]); self.assertEqual(response["observed_at"], NOW)
        self.assertEqual(response["expense_proof"]["provider_credits_expected"], 1)
        self.assertEqual(len(state["concepts"]), 1); self.assertEqual(state["market_reviews"], {})

    def test_restart_reuses_the_saved_original_without_another_credit(self):
        self.prepare(); self.checks(self.handlers())
        self.memory = ResearchMemory(self.memory.root, Journal(self.root / "finance"), evidence_type="synthetic")
        self.assertEqual(self.checks(self.handlers(), LATER)["handler_calls"], 0)
        self.assertEqual(len(self.opener.calls), 1); self.assertEqual(len(self.owners()), 1)

    def test_quickcommerce_phrases_never_become_discovery_questions(self):
        catalogue = fixture(); catalogue["families"][0]["phrases"][0]["source"] = "quickcommerce"
        before = self.memory.snapshot()
        hint = discovery_wave_readiness(self.memory, "programme", catalogue, handlers=self.handlers(), as_of=self.now)
        self.assertFalse(hint["new_wave_ready"])
        self.assertEqual(hint["preparation_gaps"], [])
        self.assertEqual(before, self.memory.snapshot())
        self.assertEqual(self.opener.calls, []); self.assertEqual(self.opener.credit_calls, [])

    def test_without_the_paid_client_the_routes_are_not_connected(self):
        self.prepare()
        handlers = installed_source_handlers(self.memory)
        self.assertFalse(any(route in handlers for route in PLATFORMS))
        result = self.checks({})
        self.assertEqual(result["counts"], {"not_connected": 1}); self.assertEqual(result["handler_calls"], 0)
        with self.assertRaisesRegex(SafetyError, "QUICKCOMMERCE_SHARED_INSTALLED_CLIENT_REQUIRED"):
            create_quickcommerce_handlers(self.memory, object())

    def test_interrupted_paid_call_is_never_reissued(self):
        self.opener.error = TimeoutError("Synthetic unknown original HTTP")
        handlers = self.handlers(); self.prepare()
        self.checks(handlers); self.opener.error = None
        self.checks(handlers, LATER); self.checks(handlers, AFTER_RECHECK)
        self.assertEqual(len(self.opener.calls), 1)
        owner = next(iter(self.owners().values()))
        self.assertFalse(self.memory.snapshot()["source_checks"][owner["active_response_id"]]["complete"])
        # The uncertain charge is not counted as a known remaining balance.
        self.assertNotIn("last_remaining", load_state(self.root)["quickcommerce"])

    def test_import_failure_recovers_saved_original_and_counts_its_credit_once(self):
        handlers = self.handlers(); self.prepare()
        with patch("product_radar.quickcommerce_credits.record_response_remaining",
                   wraps=__import__("product_radar.spend_watch_quickcommerce", fromlist=["x"]).record_response_remaining) as counted:
            with patch("product_radar.quickcommerce.save_capture", side_effect=OSError("Synthetic import failure")):
                self.checks(handlers)
            self.checks(handlers, LATER)
        self.assertEqual(len(self.opener.calls), 1); self.assertEqual(len(self.memory.snapshot()["observations"]), 1)
        self.assertEqual(counted.call_count, 1)

    def test_mismatched_provider_location_is_not_empty_or_positive_capture(self):
        bad = payload(self.request); bad["data"]["lat"] = 20
        self.opener.response = Response(json.dumps(bad).encode())
        handlers = self.handlers(); self.prepare()
        self.checks(handlers)
        self.assertEqual(len(self.opener.calls), 1); self.assertEqual(self.memory.snapshot()["observations"], {})
        owner = next(iter(self.owners().values())); response = self.memory.snapshot()["source_checks"][owner["active_response_id"]]
        self.assertFalse(response["complete"]); self.assertNotEqual(owner["state"], "completed_empty")

    def test_true_provider_empty_is_complete_without_market_vote(self):
        self.opener.response = Response(json.dumps(payload(self.request, [])).encode())
        handlers = self.handlers(); self.prepare()
        self.assertEqual(self.checks(handlers)["counts"], {"completed_no_results": 1})
        self.assertEqual(len(self.opener.calls), 1); self.assertEqual(self.memory.snapshot()["market_reviews"], {})

    def test_installed_factory_preserves_shared_original_reconciler(self):
        handlers = installed_source_handlers(self.memory, quickcommerce_client=self.client,
                                             quickcommerce_options={"spend_root": self.root})
        handlers = {"blinkit": handlers["blinkit"]}
        self.prepare()
        with patch("product_radar.quickcommerce.save_capture", side_effect=OSError("Synthetic import failure")):
            self.checks(handlers)
        self.checks(handlers, LATER)
        self.assertEqual(len(self.opener.calls), 1)
        self.assertEqual(len(self.memory.snapshot()["observations"]), 1)

    def test_provider_rate_limit_pauses_the_route_and_is_not_waived(self):
        self.prepare()
        opener = Opener(error=HTTPError("https://api.quickcommerceapi.com/v1/search", 429,
                                       "Synthetic provider limit", {}, io.BytesIO(b"")))
        handlers = self.handlers(QuickCommerceClient(SECRET, opener=opener))
        self.checks(handlers)
        self.assertEqual(self.checks(handlers, "2026-10-09T10:00:30+00:00")["handler_calls"], 0)
        self.assertEqual((len(opener.calls), self.tasks()[0]["tries"]), (1, 1))
        record = {"programme_id": "programme", "source_run_id": self.run, "concept_ids": ["sink"]}
        readiness = _source_turn_readiness(self.memory, self.memory.snapshot(), record, handlers,
                                          "2026-10-09T10:00:30+00:00")
        self.assertFalse(readiness["callback_ready"])
        self.assertEqual(len(opener.calls), 1)

    def test_paid_call_still_needs_callback_admission(self):
        self.prepare()
        policy = dict(DEFAULT_POLICY, source_callbacks=1, exploration_slots=0)
        turn = open_work_turn(self.memory, "programme", at=NOW, policy=policy)
        with bind_work_turn(self.memory, turn["id"]), \
                patch("product_radar.source_coverage.reserve_source_callback", side_effect=SafetyError("WORK_TURN_SOURCE_LIMIT")):
            result = self.checks(self.handlers())
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(self.opener.calls, [])

    def test_responses_and_adopted_origins_are_paid_provider_originals_only(self):
        from product_radar.models import digest
        from product_radar.source_acquisitions import adopt_quickcommerce_origin, record_quickcommerce_response
        self.prepare(); self.checks(self.handlers())
        aid, owner = next(iter(self.owners().items()))
        reference = str(self.memory.root / "quickcommerce_captures" / aid / "capture.json")
        paid = owner["binding"]["request"]
        for request in (dict(paid, transport="public-browser"), dict(paid, query="other words")):
            from product_radar.research_questions import physical_request_fingerprint
            binding = dict(owner["binding"], request=request, physical_request_fingerprint=physical_request_fingerprint(
                "blinkit", request, method=owner["binding"]["method"], capture_options=owner["binding"]["capture_options"],
                history=True))
            current = self.memory.snapshot()["batch_runs"][aid]
            self.memory.put("batch_runs", aid, dict(owner, binding=binding, binding_hash=digest(binding)), previous=current)
            with self.subTest(request=request), self.assertRaisesRegex(SafetyError, "SOURCE_QUICKCOMMERCE_SCOPE_INVALID"):
                record_quickcommerce_response(self.memory, aid, reference=reference, as_of=LATER)
        capture_id = next(iter(self.memory.snapshot()["captures"]))
        for request, code in ((dict(paid, transport="public-browser"), "SOURCE_QUICKCOMMERCE_ORIGIN_INVALID"),
                              (dict(paid, query="other words"), "SOURCE_QUICKCOMMERCE_ORIGIN_SCOPE_INVALID")):
            with self.subTest(request=request), self.assertRaisesRegex(SafetyError, code):
                adopt_quickcommerce_origin(self.memory, request, table="captures", record_id=capture_id, as_of=LATER,
                                           artifact_roots=[self.memory.root])

    def test_paid_request_and_saved_reply_rules(self):
        from product_radar.quickcommerce_handlers import _funded_response, prepare_funded_query
        prepared = prepare_funded_query("blinkit", {"query": "sink caddy", "location": deepcopy(MUMBAI)})
        self.assertEqual(prepared["transport"], "funded-http")
        self.assertEqual(prepare_funded_query("blinkit", prepared), prepared)
        for bad in (None, dict(prepared, transport="public-browser"), {"query": "sink caddy", "location": MUMBAI, "page": 2}):
            with self.subTest(bad=bad), self.assertRaises(SafetyError):
                prepare_funded_query("blinkit", bad)
        folder = self.root / "replies"; folder.mkdir()
        reply, intent = folder / "original_response.json", folder / "funded_intent.json"
        class Client:
            calls = 0
            def search(self, request, *, on_dispatch=None):
                self.calls += 1; on_dispatch()
                return {"request": request, "state": "captured"}
        client, replies = Client(), []
        with self.assertRaisesRegex(SafetyError, "SOURCE_ORIGINAL_RECEIPT_PENDING"):
            _funded_response(client, self.request, reply, intent, None, saved_only=True)
        first = _funded_response(client, self.request, reply, intent, None, on_reply=replies.append)
        self.assertEqual(_funded_response(client, self.request, reply, intent, None, on_reply=replies.append), first)
        self.assertEqual((client.calls, len(replies)), (1, 1))  # a saved reply is never paid or counted again
        other = prepare_query("zepto", "sink caddy", MUMBAI)
        with self.assertRaisesRegex(SafetyError, "QUICKCOMMERCE_CAPTURE_CONFLICT"):
            _funded_response(client, other, reply, intent, None)
        reply.unlink()
        with self.assertRaisesRegex(SafetyError, "QUICKCOMMERCE_CAPTURE_CONFLICT"):
            _funded_response(client, other, reply, intent, None)
        from product_radar.storage import atomic_json
        atomic_json(intent, {"request_hash": digest_of(self.request), "state": "dispatched"})
        with self.assertRaisesRegex(SafetyError, "QUICKCOMMERCE_FUNDED_RESPONSE_UNCERTAIN"):
            _funded_response(client, self.request, reply, intent, None)
        self.assertEqual(client.calls, 1)

    # ----- admission at run time ----------------------------------------------

    def test_a_type_that_becomes_held_while_waiting_on_credits_is_not_paid_for(self):
        provider = Provider(available=0)
        handlers = self.handlers(QuickCommerceClient(SECRET, opener=provider))
        self.prepare()
        self.assertEqual(self.checks(handlers)["counts"], {"waiting": 1})
        self.assertEqual(self.tasks()[0]["reason"], "QUICKCOMMERCE_CREDITS_EXHAUSTED")
        self.admission.return_value = "QUICKCOMMERCE_RESERVED_CROWDING_HIGH"  # crowding was rechecked meanwhile
        provider.available = 2500  # and the owner bought a pack
        result = self.checks(handlers, AFTER_RECHECK)
        self.assertEqual(result["counts"], {"waiting": 1}); self.assertEqual(provider.calls, [])
        task = self.tasks()[0]
        self.assertEqual((task["status"], task["tries"], task["reason"], task["next_retry_at"]),
                         ("prepared", 0, "QUICKCOMMERCE_RESERVED_CROWDING_HIGH", "2026-10-09T12:10:00+00:00"))
        self.assertEqual(len(provider.credit_calls), 1)  # a held question does not even check the balance

    def test_a_held_type_is_asked_once_it_is_admitted_again(self):
        self.admission.return_value = "QUICKCOMMERCE_RESERVED_CHECKLIST"
        handlers = self.handlers(); self.prepare()
        self.assertEqual(self.checks(handlers)["counts"], {"waiting": 1})
        self.assertEqual((self.opener.calls, self.opener.credit_calls), ([], []))
        self.admission.return_value = None
        self.assertEqual(self.checks(handlers, AFTER_RECHECK)["counts"], {"acquired": 1})
        self.assertEqual(len(self.opener.calls), 1)

    def test_any_admitted_target_lets_a_shared_question_run(self):
        self.memory.put("concepts", "other", deepcopy(CONCEPT))
        prepare_source_tasks(self.memory, [{"route": "blinkit", "request": {"query": "sink caddy", "location": deepcopy(MUMBAI)},
            "concept_ids": ["sink", "other"]}], run_id=self.run, as_of=NOW)
        self.admission.side_effect = lambda state, cid: None if cid == "other" else "QUICKCOMMERCE_RESERVED_CHECKLIST"
        self.assertEqual(self.checks(self.handlers())["counts"], {"acquired": 1})

    def test_a_later_place_waits_for_a_relevant_first_place_result(self):
        self.prepare(place=DELHI)
        provider = Provider()
        handlers = self.handlers(QuickCommerceClient(SECRET, opener=provider))
        self.assertEqual(self.checks(handlers)["counts"], {"waiting": 1})
        self.assertEqual(self.tasks()[0]["reason"], "QUICKCOMMERCE_RESERVED_FIRST_PLACE")
        self.relevance.return_value = True
        self.assertEqual(self.checks(handlers, AFTER_RECHECK)["counts"], {"acquired": 1})
        self.assertEqual(len(provider.calls), 1)

    def test_a_place_no_longer_configured_is_never_paid_for(self):
        self.prepare(place={"name": "Pune", "lat": "18.5204", "lon": "73.8567"})
        self.assertEqual(self.checks(self.handlers())["counts"], {"waiting": 1})
        self.assertEqual(self.tasks()[0]["reason"], "QUICKCOMMERCE_RESERVED_PLACE_NOT_CONFIGURED")
        self.assertEqual(self.opener.calls, [])

    def test_a_proved_unstarted_reservation_still_passes_both_gates(self):
        from product_radar import source_coverage
        hooks = source_coverage.direct_source_resolution_hooks
        def unstarted(*args, **options):
            return dict(hooks(*args, **options), resolve_source=lambda context: {"action": "collect"})
        provider = Provider(available=0)
        with patch("product_radar.source_coverage.direct_source_resolution_hooks", side_effect=unstarted):
            preflight = self.handlers(QuickCommerceClient(SECRET, opener=provider))["blinkit"]["source_access_preflight"]
        context = {"route": "blinkit", "request": {"query": "sink caddy", "location": deepcopy(MUMBAI)},
                   "reconciling": True, "as_of": NOW, "acquisition_id": "reserved-unstarted", "concept_ids": ["sink"]}
        self.assertEqual(preflight(context), {"action": "wait", "calm_wait": True, "reason": "QUICKCOMMERCE_CREDITS_EXHAUSTED",
                                              "next_retry_at": "2026-10-09T11:00:00+00:00"})
        provider.available = 2500
        self.assertEqual(preflight(dict(context, as_of=AFTER_RECHECK)), {"action": "collect"})
        self.admission.return_value = "QUICKCOMMERCE_RESERVED_CROWDING_NOT_CHECKED"
        self.assertEqual(preflight(dict(context, as_of="2026-10-09T12:00:00+00:00"))["reason"],
                         "QUICKCOMMERCE_RESERVED_CROWDING_NOT_CHECKED")
        self.assertEqual((len(provider.credit_calls), provider.calls), (2, []))

    # ----- the credit gate ----------------------------------------------------

    def test_one_free_balance_check_per_batch_then_each_reply_records_remaining(self):
        provider = Provider(available=44, remaining=41)
        self.prepare([("blinkit", "sink caddy"), ("zepto", "sink caddy"), ("instamart", "sink caddy")])
        from product_radar import quickcommerce_plan
        with patch("product_radar.quickcommerce_plan.load_settings", wraps=quickcommerce_plan.load_settings) as settings:
            result = self.checks(self.handlers(QuickCommerceClient(SECRET, opener=provider), routes=tuple(PLATFORMS)))
        self.assertEqual(result["counts"], {"acquired": 3})
        self.assertEqual((settings.call_count, self.admission.call_count), (1, 1))  # one read and answer per pass and type
        self.assertEqual((len(provider.credit_calls), len(provider.calls)), (1, 3))
        saved = load_state(self.root)["quickcommerce"]
        self.assertEqual(saved["last_reading"]["total_available"], 44)
        self.assertEqual(saved["last_remaining"]["credits_remaining"], 41)
        self.assertIsNone(credit_wait(self.root))

    def test_used_up_credits_wait_calmly_and_a_bought_pack_clears_the_wait(self):
        provider = Provider(available=0)
        handlers = self.handlers(QuickCommerceClient(SECRET, opener=provider), routes=tuple(PLATFORMS))
        self.prepare([("blinkit", "sink caddy"), ("zepto", "sink caddy")])
        result = self.checks(handlers)
        self.assertEqual(result["counts"], {"waiting": 2}); self.assertEqual(result["handler_calls"], 0)
        self.assertEqual((len(provider.credit_calls), provider.calls), (1, []))
        self.assertEqual(credit_wait(self.root)["reason"], "QUICKCOMMERCE_CREDITS_EXHAUSTED")
        for task in self.tasks():  # a calm wait with an hour's recheck, never a failure or an attempt
            self.assertEqual((task["status"], task["tries"], task["reason"]),
                             ("prepared", 0, "QUICKCOMMERCE_CREDITS_EXHAUSTED"))
            self.assertEqual(task["next_retry_at"], "2026-10-09T11:00:00+00:00")
        # Before the recheck time nothing is asked again, not even the free check.
        self.assertEqual(self.checks(handlers, LATER)["handler_calls"], 0)
        self.assertEqual(len(provider.credit_calls), 1)
        provider.available = 2500  # the owner bought a pack
        result = self.checks(handlers, AFTER_RECHECK)
        self.assertEqual(result["counts"], {"acquired": 2})
        self.assertIsNone(credit_wait(self.root))
        self.assertEqual((len(provider.credit_calls), len(provider.calls)), (2, 2))

    def test_unknown_balance_fails_closed_with_a_short_recheck(self):
        provider = Provider(available=TimeoutError("Synthetic network failure"))
        self.prepare()
        result = self.checks(self.handlers(QuickCommerceClient(SECRET, opener=provider)))
        self.assertEqual(result["counts"], {"waiting": 1}); self.assertEqual(provider.calls, [])
        task = self.tasks()[0]
        self.assertEqual((task["reason"], task["next_retry_at"]), ("QUICKCOMMERCE_NETWORK_FAILED", "2026-10-09T10:10:00+00:00"))
        self.assertEqual(load_state(self.root)["quickcommerce"]["last_check_failure"]["reason"], "QUICKCOMMERCE_NETWORK_FAILED")

    def test_a_reply_that_uses_the_last_credit_stops_the_rest_of_the_batch(self):
        provider = Provider(available=1, remaining=0)
        self.prepare([("blinkit", "sink caddy"), ("zepto", "sink caddy")])
        result = self.checks(self.handlers(QuickCommerceClient(SECRET, opener=provider), routes=("blinkit", "zepto")))
        self.assertEqual(result["counts"], {"acquired": 1, "waiting": 1})
        self.assertEqual(len(provider.calls), 1)
        # The used-up reply set the wait; the free check ran again and kept it.
        self.assertEqual(len(provider.credit_calls), 2)
        self.assertEqual(credit_wait(self.root)["reason"], "QUICKCOMMERCE_CREDITS_EXHAUSTED")

    def test_a_no_credits_refusal_with_a_positive_balance_keeps_the_gate_closed_until_it_rises(self):
        provider = Provider(available=5, status=402)
        handlers = self.handlers(QuickCommerceClient(SECRET, opener=provider), routes=("blinkit", "zepto"))
        self.prepare([("blinkit", "sink caddy"), ("zepto", "sink caddy")])
        result = self.checks(handlers)
        # The refusal closes the gate for the rest of the pass although the balance still says 5.
        self.assertEqual(result["counts"], {"blocked": 1, "waiting": 1}); self.assertEqual(len(provider.calls), 1)
        self.assertEqual(credit_wait(self.root)["reason"], "QUICKCOMMERCE_CREDITS_EXHAUSTED")
        refused = next(task for task in self.tasks() if task["status"] == "blocked")
        self.assertEqual((refused["tries"], refused["retryable"], len(refused["attempt_refs"])), (0, True, 1))
        self.assertEqual(refused["credit_refusal_refs"], refused["attempt_refs"])
        run = next(row for row in self.memory.snapshot()["batch_runs"].values() if row.get("run_id") == self.run)
        self.assertEqual(run.get("route_pauses", {}), {})  # no source-wide pause: the spend watch owns the wait
        provider.status = 200
        # The same reported balance is not trusted: still closed after the recheck.
        self.assertEqual(self.checks(handlers, AFTER_RECHECK)["handler_calls"], 0)
        self.assertEqual(len(provider.calls), 1)
        provider.available = 2505  # a pack was bought
        result = self.checks(handlers, "2026-10-09T12:20:00+00:00")
        self.assertEqual(result["counts"], {"acquired": 2})
        self.assertIsNone(credit_wait(self.root))
        retried = next(task for task in self.tasks() if task["route"] == refused["route"])
        self.assertEqual((retried["tries"], len(retried["attempt_refs"])), (1, 2))
        self.assertNotEqual(retried["acquisition_id"], refused["acquisition_id"])


if __name__ == "__main__":
    unittest.main()
