"""Real installed receipt adapters with synthetic original browser bodies only."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from product_radar.catalogue_discovery import advance_discovery_cycle, prepare_discovery_cycle
from product_radar.marketplace_browser import build_marketplace_request, capture_marketplace, replay_marketplace
from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory, key
from product_radar.source_acquisitions import ACQUISITION_KIND, claim_source_acquisition
from product_radar.source_coverage import (ACCESS_SCOPE_KIND, _access_backoff_seconds, _access_failure, _source_retry_at,
    create_marketplace_handlers,
    execute_source_run, marketplace_access_admission, prepare_source_tasks, source_access_backoffs, source_access_scope,
    source_access_wait)
from product_radar.storage import Journal, atomic_json
from product_radar.work_turns import DEFAULT_POLICY, bind_work_turn, open_work_turn
from test_catalogue_expansion import fixture
from test_marketplace_browser import snapshot, transport


NOW = "2026-10-09T10:00:00+00:00"
DUE = "2026-10-09T11:00:00+00:00"


class SourceAccessWaitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.memory.put("concepts", "sink", {"status": "active", "description": {"label": "Sink caddy",
            "category": "Home and Kitchen", "function": "Store sink essentials", "mechanism": "manual",
            "audience": "adult", "features": ["draining"], "brand": "generic_or_small",
            "basis": "observed", "reason": "Synthetic original source fixture."}})
        self.calls = []; self.now = NOW; self.status = 429; self.header = "3600"; self.changes = {}
        self.failure = None; self.closed = True
        self.source_root = self.memory.root / "direct_marketplace"
        def capture(request, acquisition_id, **options):
            self.calls.append((request["route"], acquisition_id, deepcopy(request)))
            if self.failure: raise self.failure
            status = 200 if request["route"] == "flipkart-search" else self.status
            changes = dict(self.changes)
            if status == 429: changes["text"] = "Too many requests"
            with patch("product_radar.marketplace_browser.utc_now", return_value=self.now):
                return capture_marketplace(request, acquisition_id, **options, snapshot_reader=lambda req:
                    transport(snapshot(req, **changes), navigation_started_at=self.now,
                              navigation_status=status, owned_process_exited=self.closed,
                              retry_after=self.header if status == 429 else None))
        self.handlers = create_marketplace_handlers(self.memory, data_root=self.source_root,
                                                   capture=capture, shared_acquisitions=True)

    def prepare(self, name, *routes, term="sink caddy"):
        rows = [{"route": route, "request": build_marketplace_request(route, department="home-and-kitchen")
                 if route == "amazon-bestsellers"
                 else {"query": term, "country": "IN"}, "concept_ids": ["sink"]}
                for route in routes]
        prepare_source_tasks(self.memory, rows, run_id=name, as_of=self.now)
        return name

    def execute(self, run, **options):
        with patch("product_radar.source_coverage.utc_now", return_value=self.now):
            return execute_source_run(self.memory, run, handlers=self.handlers, as_of=self.now,
                bounded_call=lambda cb, context, _timeout: cb(context), shared_marketplace=True, **options)

    def wait(self):
        scope = source_access_scope("https://www.amazon.in/s?k=sink")
        return source_access_wait(self.memory.snapshot(), scope, as_of=self.now)

    def test_first_429_suppresses_other_amazon_routes_but_flipkart_continues(self):
        self.execute(self.prepare("first", "amazon-search"))
        result = self.execute(self.prepare("rest", "amazon-bestsellers", "flipkart-search"))
        self.assertEqual(result["handler_calls"], 1)
        self.assertEqual([r[0] for r in self.calls], ["amazon-search", "flipkart-search"])
        # Amazon and Flipkart are crowding reads only (D4): discovery plans none of them.
        cycle = prepare_discovery_cycle(self.memory, "discovery", fixture(), handlers=self.handlers,
                                        action_limit=2, as_of=self.now)
        self.assertEqual(cycle["planned_actions"], [])
        result = advance_discovery_cycle(self.memory, cycle["id"], handlers=self.handlers, as_of=self.now,
                                         admission=lambda *_a, **_k: None, shared_marketplace=True)
        self.assertEqual(result["external_handler_calls_this_round"], 0)
        self.assertEqual(self.wait()["next_retry_at"], DUE)
        self.assertEqual(self.wait()["phase"], "waiting")

    def test_same_original_replay_during_wait_uses_no_callback_or_new_attempt(self):
        self.execute(self.prepare("first", "amazon-search"))
        before = len(self.calls)
        turn = open_work_turn(self.memory, "programme", at=self.now)
        with bind_work_turn(self.memory, turn["id"]):
            result = self.execute(self.prepare("other-consumer", "amazon-search"))
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(len(self.calls), before)
        self.assertEqual(self.memory.snapshot()["batch_runs"][turn["id"]]["counters"]["source_callbacks"], 0)

    def test_saved_complete_other_question_stays_ready_for_local_apply_during_wait(self):
        self.status = 200
        self.execute(self.prepare("healthy", "amazon-search"))
        self.status = 429
        self.execute(self.prepare("blocked-other", "amazon-search", term="garbage bags"))
        helper = self.handlers["amazon-search"]["marketplace_due_hint"]
        hint = helper({"route": "amazon-search", "request": {"query": "sink caddy"}, "as_of": NOW})
        self.assertTrue(hint["ready"])
        self.assertTrue(hint["local_saved"])
        self.assertEqual(hint["signal"]["wait"]["phase"], "waiting")

    def test_due_original_probe_is_linked_generation_and_second_429_waits_again(self):
        run = self.prepare("first", "amazon-search"); self.execute(run)
        original = self.calls[0][1]
        self.now = DUE
        result = self.execute(run)
        self.assertEqual(result["handler_calls"], 1)
        self.assertEqual(len(self.calls), 2)
        owner = self.memory.snapshot()["batch_runs"][self.calls[-1][1]]
        self.assertEqual(owner["binding"]["previous_acquisition_id"], original)
        self.assertEqual(owner["binding"]["generation"], 2)
        self.assertEqual(self.wait()["next_retry_at"], "2026-10-09T12:00:00+00:00")
        self.execute(self.prepare("new-list", "amazon-bestsellers"))
        self.assertEqual(len(self.calls), 2)

    def test_list_first_refusal_also_suppresses_product_search_requests(self):
        result = self.execute(self.prepare("list-first", "amazon-bestsellers"))
        self.assertEqual(result["handler_calls"], 1)
        self.execute(self.prepare("searches", "amazon-search", "flipkart-search"))
        self.assertEqual([r[0] for r in self.calls], ["amazon-bestsellers", "flipkart-search"])

    def active_failure(self):
        state = self.memory.snapshot()
        return _access_failure(state, self.wait()["failure_id"])["binding"]

    def test_consecutive_refusals_escalate_to_a_capped_day_and_a_success_resets(self):
        # Repair 7: a permanent refusal is no longer probed every five minutes.
        self.header = None
        expected = [("2026-10-09T10:00:00+00:00", "2026-10-09T10:05:00+00:00"),
                    ("2026-10-09T10:05:00+00:00", "2026-10-09T11:05:00+00:00"),
                    ("2026-10-09T11:05:00+00:00", "2026-10-09T17:05:00+00:00"),
                    ("2026-10-09T17:05:00+00:00", "2026-10-10T17:05:00+00:00"),
                    ("2026-10-10T17:05:00+00:00", "2026-10-11T17:05:00+00:00")]
        for attempt, (at, due) in enumerate(expected, 1):
            with self.subTest(attempt=attempt):
                self.now = at
                # The due probe of a new question is the only request sent.
                self.assertEqual(self.execute(self.prepare("probe-" + str(attempt), "amazon-search",
                                                           term="sink caddy " + str(attempt)))["handler_calls"], 1)
                self.assertEqual((self.active_failure()["attempt"], self.wait()["next_retry_at"]), (attempt, due))
                held = self.execute(self.prepare("held-" + str(attempt), "amazon-search", term="held " + str(attempt)))
                self.assertEqual(held["handler_calls"], 0)  # Still waiting: no new request.
        self.assertEqual(source_access_backoffs(self.memory.snapshot(), as_of=self.now),
            [{"host": "www.amazon.in", "transport": "public-browser", "attempt": 5, "wait_seconds": 86400,
              "challenge": True, "reason": None, "http_status": 429,  # A plain refusal saves no page reason.
              "phase": "waiting", "next_retry_at": "2026-10-11T17:05:00+00:00"}])
        # A successful read clears the wait and starts the count again.
        self.now = "2026-10-11T17:05:00+00:00"; self.status = 200
        self.assertEqual(self.execute(self.prepare("healthy", "amazon-search", term="healthy"))["handler_calls"], 1)
        self.assertIsNone(self.wait())
        self.assertEqual(source_access_backoffs(self.memory.snapshot(), as_of=self.now), [])
        self.now = "2026-10-11T18:00:00+00:00"; self.status = 429
        self.execute(self.prepare("after-success", "amazon-search", term="garbage bags"))
        self.assertEqual((self.active_failure()["attempt"], self.wait()["next_retry_at"]), (1, "2026-10-11T18:05:00+00:00"))
        self.assertEqual(len(self.calls), 7)  # Five refusals, one success, one fresh refusal.

    def test_escalation_never_shortens_a_longer_retry_after(self):
        self.header = None
        run = self.prepare("first", "amazon-search"); self.execute(run)
        self.now = "2026-10-09T10:05:00+00:00"; self.header = "7200"
        self.execute(run)
        self.assertEqual((self.active_failure()["attempt"], self.wait()["next_retry_at"]), (2, "2026-10-09T12:05:00+00:00"))

    def test_records_saved_before_escalation_stay_valid_and_counts_are_bound(self):
        self.header = None
        self.execute(self.prepare("first", "amazon-search"))
        state = self.memory.snapshot(); failure_id = self.wait()["failure_id"]
        record = state["source_checks"][failure_id]
        self.assertEqual((record["binding"]["attempt"], record["binding"]["wait_seconds"]), (1, 300))
        def saved(binding):
            copy = {**state, "source_checks": dict(state["source_checks"])}
            new_id = key("source-access-refusal", binding)
            copy["source_checks"][new_id] = {**record, "binding": binding, "binding_hash": digest(binding)}
            return copy, new_id
        # An older first refusal has neither field and the same five minutes.
        old = {k: v for k, v in record["binding"].items() if k not in ("attempt", "wait_seconds")}
        self.assertEqual(_access_failure(*saved(old))["binding"], old)
        # A record is checked against its own saved wait, not today's steps:
        # a sixth refusal saved with a two-hour wait stays valid.
        retuned = dict(record["binding"], attempt=6, wait_seconds=7200, next_retry_at="2026-10-09T12:00:00+00:00")
        self.assertEqual(_access_failure(*saved(retuned))["binding"], retuned)
        # A wait that does not match its saved end, or half a pair, is not proof.
        for index, changed in enumerate((dict(record["binding"], wait_seconds=3600),
                                         dict(record["binding"], attempt=0), dict(record["binding"], attempt=True),
                                         {k: v for k, v in record["binding"].items() if k != "wait_seconds"})):
            with self.subTest(variant=index), self.assertRaisesRegex(SafetyError, "SOURCE_ACCESS_PROOF_INVALID"):
                _access_failure(*saved(changed))

    def test_plain_server_errors_escalate_but_stop_at_one_hour(self):
        self.header = None; self.status = 502
        for attempt, (at, due) in enumerate((("2026-10-09T10:00:00+00:00", "2026-10-09T10:05:00+00:00"),
                                             ("2026-10-09T10:05:00+00:00", "2026-10-09T11:05:00+00:00"),
                                             ("2026-10-09T11:05:00+00:00", "2026-10-09T12:05:00+00:00")), 1):
            with self.subTest(attempt=attempt):
                self.now = at
                self.execute(self.prepare("probe-" + str(attempt), "amazon-search", term="sink caddy " + str(attempt)))
                failure = self.active_failure()
                self.assertEqual((failure["attempt"], failure["challenge"], self.wait()["next_retry_at"]),
                                 (attempt, False, due))

    def test_product_image_hosts_stop_at_one_hour_and_challenges_at_a_day(self):
        self.assertEqual([_access_backoff_seconds(n, challenge=True, image=False) for n in range(1, 7)],
                         [300, 3600, 21600, 86400, 86400, 86400])
        for challenge, image in ((False, False), (True, True), (False, True)):
            self.assertEqual([_access_backoff_seconds(n, challenge=challenge, image=image) for n in range(1, 5)],
                             [300, 3600, 3600, 3600])

    def test_late_recorded_refusal_from_an_earlier_read_does_not_escalate(self):
        # Another lane's read was refused at 09:58 but its result is recorded
        # only after a 10:00 refusal already set the wait.
        from product_radar import source_coverage
        self.header = None; self.now = "2026-10-09T09:58:00+00:00"
        with patch.object(source_coverage, "_record_marketplace_access"):
            self.execute(self.prepare("other-lane", "amazon-bestsellers"))
        late_aid = self.calls[-1][1]
        self.now = NOW
        self.execute(self.prepare("first", "amazon-search"))
        self.assertEqual((self.active_failure()["attempt"], self.wait()["next_retry_at"]), (1, "2026-10-09T10:05:00+00:00"))
        owner = self.memory.snapshot()["batch_runs"][late_aid]
        source_coverage._record_marketplace_access(self.memory, late_aid, owner["active_response_id"], as_of=NOW)
        state = self.memory.snapshot()
        late = [_access_failure(state, fid)["binding"] for fid in state["batch_runs"][self.wait()["scope_id"]]["failure_ids"]
                if _access_failure(state, fid)["binding"]["acquisition_id"] == late_aid]
        self.assertEqual([(row["attempt"], row["next_retry_at"]) for row in late], [(1, "2026-10-09T10:03:00+00:00")])
        # The current wait, and its count, are unchanged; replay stays inert.
        self.assertEqual((self.active_failure()["attempt"], self.wait()["next_retry_at"]), (1, "2026-10-09T10:05:00+00:00"))
        source_coverage._record_marketplace_access(self.memory, late_aid, owner["active_response_id"], as_of=NOW)
        self.assertEqual(self.memory.snapshot()["source_checks"], state["source_checks"])

    def test_due_probe_answered_without_a_refusal_clears_the_wait(self):
        # A 404 (or any other non-refusal answer) proves the host answered.
        run = self.prepare("first", "amazon-search"); self.execute(run)
        self.now = DUE; self.status = 404
        self.assertEqual(self.execute(run)["handler_calls"], 1)
        self.assertIsNone(self.wait())
        self.status = 200
        self.assertEqual(self.execute(self.prepare("next", "amazon-bestsellers"))["handler_calls"], 1)

    def test_due_probe_with_no_answer_releases_the_probe_but_keeps_the_wait(self):
        run = self.prepare("first", "amazon-search"); self.execute(run)
        refusal = self.wait()["failure_id"]
        self.now = DUE
        def unanswered(request, acquisition_id, **options):
            self.calls.append((request["route"], acquisition_id, deepcopy(request)))
            with patch("product_radar.marketplace_browser.utc_now", return_value=self.now):
                return capture_marketplace(request, acquisition_id, **options, snapshot_reader=lambda req: transport(
                    None, state="failed", navigation_started_at=self.now, navigation_status=None))
        self.handlers = create_marketplace_handlers(self.memory, data_root=self.source_root,
                                                   capture=unanswered, shared_acquisitions=True)
        self.assertEqual(self.execute(run)["handler_calls"], 1)
        wait = self.wait()
        # Not cleared (the host never answered), but no longer held by a probe forever.
        self.assertEqual((wait["failure_id"], wait["phase"], wait["probe"]), (refusal, "due", None))
        self.assertEqual(self.execute(self.prepare("next-probe", "amazon-search", term="other"))["handler_calls"], 1)
        self.assertEqual(len(self.calls), 3)

    def test_successful_due_probe_clears_host_wait(self):
        run = self.prepare("first", "amazon-search"); self.execute(run)
        self.now = DUE; self.status = 200
        self.execute(run)
        self.assertIsNone(self.wait())
        self.execute(self.prepare("new", "amazon-bestsellers"))
        self.assertEqual(len(self.calls), 3)

    def test_new_caller_due_probe_supersedes_only_its_exact_old_route_refusal(self):
        self.execute(self.prepare("first", "amazon-search"))
        original = self.calls[0][1]
        self.now = DUE; self.status = 200
        result = self.execute(self.prepare("new-owner", "amazon-search"))
        self.assertEqual(result["counts"], {"acquired": 1})
        self.assertEqual(len(self.calls), 2)
        owner = self.memory.snapshot()["batch_runs"][self.calls[-1][1]]
        self.assertEqual(owner["binding"]["previous_acquisition_id"], original)
        self.assertIsNone(self.wait())
        # The original controller still retains its immutable old receipt;
        # its proved successful host recovery must not strand healthy work.
        self.execute(self.prepare("after-recovery", "amazon-search", term="fresh storage"))
        self.assertEqual(len(self.calls), 3)

    def test_one_scope_probe_claim_is_joined_cas_not_a_repeat_launch_voucher(self):
        self.execute(self.prepare("first", "amazon-search")); self.now = DUE
        resolved = []
        for caller, route in (("list-caller", "amazon-bestsellers"), ("search-caller", "amazon-search")):
            request = (build_marketplace_request(route, department="home-and-kitchen") if route == "amazon-bestsellers"
                       else build_marketplace_request(route, term="garbage bags"))
            context = {"task_id": caller, "route": route, "request": request,
                       "acquisition_id": caller, "as_of": self.now, "concept_ids": [], "description_hashes": {}}
            resolution = self.handlers[route]["resolve_marketplace"](context)
            admission = self.handlers[route]["marketplace_admission"](context, resolution)
            resolved.append(admission)
        self.memory.commit("first-probe-joined-admission", resolved[0]["changes"])
        with self.assertRaisesRegex(SafetyError, "RESEARCH_DEPENDENCY_CHANGED"):
            self.memory.commit("second-probe-joined-admission", resolved[1]["changes"])
        self.assertEqual(self.wait()["phase"], "probe_pending")
        self.assertEqual(len(self.calls), 1)

    def test_exhausted_turn_does_not_reserve_scope_probe(self):
        run = self.prepare("first", "amazon-search"); self.execute(run); self.now = DUE
        policy = deepcopy(DEFAULT_POLICY); policy["source_callbacks"] = 1; policy["exploration_slots"] = 0
        turn = open_work_turn(self.memory, "limited", at=self.now, policy=policy)
        from product_radar.work_turns import reserve_source_callback
        with bind_work_turn(self.memory, turn["id"]):
            reserve_source_callback(self.memory, "used-slot", at=self.now)
            self.execute(run)
        self.assertEqual(self.wait()["phase"], "due")
        self.assertEqual(len(self.calls), 1)

    def test_parser_error_does_not_pause_healthy_host(self):
        self.status = 200; self.changes = {"cards": "bad local parse shape"}
        self.execute(self.prepare("first", "amazon-search"))
        self.assertIsNone(self.wait())
        self.changes = {}
        self.execute(self.prepare("healthy", "amazon-bestsellers"))
        self.assertEqual(len(self.calls), 2)

    def test_actual_retry_header_is_preserved_and_replay_binds_it(self):
        self.execute(self.prepare("first", "amazon-search"))
        folder = self.source_root / self.calls[0][1]
        receipt = replay_marketplace(folder)
        self.assertEqual(receipt["retry_after"], "3600")
        receipt["retry_after"] = "1"; atomic_json(folder / "receipt.json", receipt)
        with self.assertRaisesRegex(SafetyError, "MARKETPLACE_TRANSPORT_MISMATCH"):
            replay_marketplace(folder)

    def test_seconds_date_and_malformed_retry_after(self):
        self.assertEqual(_source_retry_at("3600", NOW), DUE)
        self.assertEqual(_source_retry_at("Fri, 09 Oct 2026 11:00:00 GMT", NOW), DUE)
        self.assertEqual(_source_retry_at("bad\r\nheader", NOW), "2026-10-09T10:05:00+00:00")

    def test_damaged_scope_lookup_cannot_hide_existing_wait(self):
        self.execute(self.prepare("first", "amazon-search"))
        state = self.memory.snapshot()
        rid, row = next((i, r) for i, r in state["batch_runs"].items() if r.get("kind") == ACCESS_SCOPE_KIND)
        self.memory.put("batch_runs", rid, dict(row, scope_hash="0" * 64), previous=row)
        result = self.execute(self.prepare("rest", "amazon-bestsellers"))
        self.assertIn("SOURCE_ACCESS_RECORD_INVALID", str(result))
        self.assertEqual(len(self.calls), 1)

    def test_due_hint_is_pure_and_wakes_once_at_actual_header_time(self):
        self.execute(self.prepare("first", "amazon-search"))
        helper = self.handlers["amazon-bestsellers"]["marketplace_due_hint"]
        context = {"route": "amazon-bestsellers", "request": build_marketplace_request("amazon-bestsellers", department="home-and-kitchen"), "as_of": NOW}
        before = self.memory.snapshot()
        self.assertFalse(helper(context)["ready"])
        self.assertTrue(helper(dict(context, as_of=DUE))["ready"])
        self.assertEqual(self.memory.snapshot(), before)

    def test_probe_missing_original_return_never_authorizes_another_host_request(self):
        run = self.prepare("first", "amazon-search"); self.execute(run)
        self.now = DUE; self.failure = TimeoutError("Synthetic unknown original probe")
        self.execute(run)
        self.assertEqual(self.wait()["phase"], "probe_pending")
        self.failure = None
        self.execute(self.prepare("other", "amazon-bestsellers", "flipkart-search"))
        self.assertEqual([r[0] for r in self.calls], ["amazon-search", "amazon-search", "flipkart-search"])
        self.execute(run)
        self.assertEqual(len(self.calls), 3)

    def test_real_429_with_unverified_cleanup_pauses_host_but_cannot_probe(self):
        self.closed = False
        self.execute(self.prepare("first", "amazon-search"))
        self.assertEqual(self.wait()["phase"], "execution_unknown")
        self.now = DUE; self.closed = True
        self.execute(self.prepare("other", "amazon-bestsellers", "flipkart-search"))
        self.assertEqual([r[0] for r in self.calls], ["amazon-search", "flipkart-search"])

    def test_successful_scope_cannot_be_forged_by_removing_active_failure(self):
        self.execute(self.prepare("first", "amazon-search"))
        rid, row = next((i, r) for i, r in self.memory.snapshot()["batch_runs"].items()
                        if r.get("kind") == ACCESS_SCOPE_KIND)
        changed = dict(row, active_failure_id=None)
        changed["record_hash"] = digest({k: v for k, v in changed.items() if k != "record_hash"})
        self.memory.put("batch_runs", rid, changed, previous=row)
        with self.assertRaisesRegex(SafetyError, "SOURCE_ACCESS_RECORD_INVALID"):
            self.wait()


if __name__ == "__main__": unittest.main()
