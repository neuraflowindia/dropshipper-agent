"""Nearly report-ready types are finished before new discovery (Plan R10 W9, P3).

Disposable accepted-type records, synthetic answers and patched review calls
only. Nothing contacts a provider, runs a model or touches the live store.
"""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import product_radar.programme as programme
from product_radar.fit_description import FUNCTIONAL_CONTRACT
from product_radar.fit_policy import FIT_POLICY_ID
from product_radar.models import SafetyError, digest, timestamp
from product_radar.nearly_ready import (NEARLY_READY_STAGES, finishing_work, passing_checklist, report_supply_short)
from product_radar.research_ai import Jobs
from product_radar.work_turns import DEFAULT_POLICY
from product_radar.inventory_intake import WORK_KIND
from product_radar.product_queries import ensure_query_profiles
from product_radar.report_selection import build_report_pool
from product_radar.source_interpretation import review_source_results, review_supplier_results
from product_radar.supplier_capture_store import capture_saved_supplier_bundle
import test_functional_fit as fixtures
import test_functional_queries as query_fixtures
from test_fit_pipeline import current_fixture
from test_report_selection import NOW as POOL_NOW
from test_research_flow import FakeRunner, NOW
import test_source_interpretation as source_fixtures
from test_supplier_capture_store import Reader, BASE as SUPPLIER_BASE
from test_suppliers import FixtureRunner

FINISHING = {stage: None for stage in NEARLY_READY_STAGES}


class NearlyReadyRuleTests(unittest.TestCase):
    def test_only_a_saved_zero_shortfall_is_enough_supply(self):
        self.assertTrue(report_supply_short({}))
        self.assertTrue(report_supply_short({"inventory_release_need": None}))
        self.assertTrue(report_supply_short({"inventory_release_need": {"shortfall": 4}}))
        self.assertTrue(report_supply_short({"inventory_release_need": {"shortfall": False}}))
        self.assertTrue(report_supply_short({"inventory_release_need": {"shortfall": "0"}}))
        self.assertFalse(report_supply_short({"inventory_release_need": {"shortfall": 0, "upcoming_issue_ids": []}}))

    def test_passing_means_five_meets_under_the_current_functional_contract(self):
        state = {"assessments": {"c": {"contract_version": FUNCTIONAL_CONTRACT}}}
        self.assertFalse(passing_checklist(state, "c", None))
        self.assertFalse(passing_checklist(state, "c", {"eligible": False}))
        self.assertTrue(passing_checklist(state, "c", {"eligible": True}))
        state["assessments"]["c"]["contract_version"] = "research-e-fit10-functional-v4"
        self.assertFalse(passing_checklist(state, "c", {"eligible": True}))

    def test_crowding_check_has_one_reserved_place_between_review_and_suppliers(self):
        self.assertEqual(NEARLY_READY_STAGES, ("acquire", "match", "crowding", "suppliers"))
        self.assertTrue(set(NEARLY_READY_STAGES) <= set(programme.STAGES))


class DiscoveryHoldTests(unittest.TestCase):
    def test_hold_is_bounded_scoped_and_clamped(self):
        from product_radar.work_turns import (DISCOVERY_RESERVED_EXECUTIONS, discovery_reserve_scope,
                                              discovery_reserved_slots, hold_for_discovery)
        fixture = fixtures.FunctionalFitTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        memory = fixture.memory
        turn = {"policy": dict(DEFAULT_POLICY)}
        hold_for_discovery(memory, 2)  # No bound round: nothing to hold.
        self.assertEqual(discovery_reserved_slots(memory, turn), 0)
        with discovery_reserve_scope(memory):
            self.assertEqual(discovery_reserved_slots(memory, turn), 0)
            hold_for_discovery(memory, DISCOVERY_RESERVED_EXECUTIONS)
            self.assertEqual(discovery_reserved_slots(memory, turn), 2)
            tiny = {"policy": dict(DEFAULT_POLICY, model_executions=3, report_slots=1, review_slots=0)}
            self.assertEqual(discovery_reserved_slots(memory, tiny), 1)  # One ordinary call always remains.
            for bad in (3, -1, True, "2"):
                with self.assertRaisesRegex(SafetyError, "WORK_TURN_DISCOVERY_RESERVE_INVALID"):
                    hold_for_discovery(memory, bad)
        self.assertEqual(discovery_reserved_slots(memory, turn), 0)


class SupplierQueueTests(unittest.TestCase):
    def pool(self, memory):
        return build_report_pool(memory, POOL_NOW, fit_policy_id=FIT_POLICY_ID)

    def test_every_passing_type_queues_supplier_search_beside_market_work(self):
        memory = current_fixture(2)
        for state in (memory.state["market_reviews"], memory.state["supplier_assessments"]):
            state.clear()
        pool = self.pool(memory)
        self.assertEqual([row["concept_id"] for row in pool["needed_supplier_work"]], ["c000", "c001"])
        self.assertEqual([row["concept_id"] for row in pool["needed_market_work"]], ["c000", "c001"])
        for decision in pool["decisions"]:
            self.assertIn("supplier_search_required", decision["codes"])
            self.assertGreater(len(decision["codes"]), 1)  # Not the last missing piece.
        self.assertTrue(all(row["blocking"] for row in pool["needed_supplier_work"]))

    def test_unpassed_or_published_types_do_not_queue_supplier_search(self):
        memory = current_fixture(1, matches=4); memory.state["supplier_assessments"].clear()
        self.assertEqual(self.pool(memory)["needed_supplier_work"], [])
        memory = current_fixture(1); memory.state["supplier_assessments"].clear()
        memory.state["published_history"]["published0"] = {"status": "published", "client_id": "default", "concept_id": "c000"}
        self.assertEqual(self.pool(memory)["needed_supplier_work"], [])


def all_work(state, scope, candidates, *, as_of):
    return {stage: set(candidates) for stage in NEARLY_READY_STAGES}


class FinishingWorkTests(unittest.TestCase):
    """Pending-work tests read saved task rows; the market check uses its owner."""
    def state(self, *tasks):
        return {"query_tasks": {"task-" + str(n): row for n, row in enumerate(tasks)}}

    def work(self, state, candidates=("a", "b"), questions=(), crowding=()):
        with patch("product_radar.source_interpretation.functional_source_questions", return_value=list(questions)) as asked, \
                patch("product_radar.crowding.crowding_due", side_effect=lambda _state, cid, **_: cid in crowding):
            value = finishing_work(state, {"source_run_id": "run"}, list(candidates), as_of=NOW)
        if candidates:
            self.assertEqual(asked.call_args.kwargs["concept_ids"], sorted(candidates))
        return value

    def test_no_candidates_reads_nothing(self):
        self.assertEqual(self.work(self.state(), candidates=()), {stage: set() for stage in FINISHING})

    def test_each_stage_needs_its_own_actual_pending_work(self):
        later = (timestamp(NOW) + timedelta(hours=1)).isoformat()
        state = self.state(
            {"kind": "all_source_task_v1", "run_id": "run", "route": "amazon-search", "targets": {"a": "h"}, "status": "acquired"},
            {"kind": "all_source_task_v1", "run_id": "run", "route": "flipkart-search", "targets": {"b": "h"},
             "status": "failed", "next_retry_at": later},
            # A deleted route's saved question (Movers & Shakers) is history, not work.
            {"kind": "all_source_task_v1", "run_id": "run", "route": "amazon-movers-and-shakers", "targets": {"b": "h"},
             "status": "prepared"},
            {"kind": "all_source_task_v1", "run_id": "run", "route": "meta-ads", "targets": {"c": "h"}, "status": "prepared"},
            {"kind": "all_source_task_v1", "run_id": "other", "route": "indiamart", "source": "supplier", "targets": {"a": "h"},
             "status": "acquired", "target_results": {"a": {"state": "interpretation_pending"}}},
            {"kind": "all_source_task_v1", "run_id": "run", "route": "indiamart", "source": "supplier", "targets": {"b": "h"},
             "status": "acquired", "target_results": {"b": {"state": "completed_relevant"}}},
            {"kind": "other-kind", "targets": {"a": "h"}})
        value = self.work(state, candidates=("a", "b", "c", "d"),
                          questions=[{"concept_id": "a", "unknown_ids": ["x"]}, {"concept_id": "b", "unknown_ids": []}],
                          crowding=("b",))
        self.assertEqual(value["acquire"], {"c", "d"})  # Open task; none prepared yet for d.
        self.assertEqual(value["match"], {"a"})
        self.assertEqual(value["crowding"], {"b"})  # Saved reads its crowding answer has not taken in.
        self.assertEqual(value["suppliers"], {"a"})
        reconcile = self.state({"kind": "all_source_task_v1", "run_id": "run", "route": "amazon-search", "targets": {"a": "h"},
                                "status": "acquired", "reconcile_required": True})
        self.assertEqual(self.work(reconcile, candidates=("a",))["acquire"], {"a"})

    def test_waits_closed_reads_and_replaced_targets_are_not_pending_work(self):
        from product_radar.research_memory import key
        later = (timestamp(NOW) + timedelta(minutes=2)).isoformat()
        earlier = (timestamp(NOW) - timedelta(minutes=2)).isoformat()
        base = {"kind": "all_source_task_v1", "run_id": "run", "route": "indiamart", "source": "supplier"}
        old = {"query": "q", "country": "IN", "transport": "supplier-public"}
        new = {"query": "q", "country": "IN", "transport": "signed-in-browser"}
        tasks = {
            # Calm signed-in waits keep their recheck time; a pacing hold its own.
            "sign-in-wait": dict(base, targets={"a": "h"}, status="prepared", reason="OWNER_SIGN_IN_REQUIRED", next_retry_at=later),
            "paced": dict(base, targets={"g": "h"}, status="prepared", reason="SIGNED_IN_READ_SPACING", next_retry_at=later),
            # A replaced read is closed for good; its replacement already acquired.
            "not-connected": dict(base, targets={"b": "h"}, status="blocked", retryable=False,
                                  reason="SUPPLIER_ROUTE_TRANSPORT_REPLACED", next_retry_at=None),
            "replacement-b": dict(base, targets={"b": "h"}, status="acquired"),
            "final-fail": dict(base, targets={"c": "h"}, status="failed", retryable=False, next_retry_at=None),
            "ok-c": dict(base, targets={"c": "h"}, status="acquired"),
            "superseded-d": dict(base, targets={"d": "h"}, status="prepared", next_retry_at=None, request=old,
                                 superseded_targets={"d": {"task_id": "new-d", "request_hash": "rh"}}),
            "new-d": dict(base, targets={"d": "h"}, status="acquired", request=new, request_hash="rh"),
            "used-up": dict(base, targets={"e": "h"}, status="failed", retryable=True, tries=2, next_retry_at=earlier),
            "not-applicable": dict(base, targets={"f": "h"}, status="not_applicable"),
            "pending-reading": dict(base, targets={"i": "h"}, status="acquired",
                                    target_results={"i": {"state": "interpretation_pending"}}),
            # Real work: a due retry, an open dispatch, a due pacing hold.
            "due-retry": dict(base, targets={"h": "h"}, status="blocked", retryable=True, next_retry_at=earlier),
            "dispatching": dict(base, targets={"j": "h"}, status="dispatching", next_retry_at=later),
            "due-hold": dict(base, targets={"k": "h"}, status="prepared", next_retry_at=earlier),
            "not-yet": dict(base, targets={"l": "h"}, status="failed", retryable=True, next_retry_at=later),
            "no-time": dict(base, targets={"m": "h"}, status="failed", retryable=True, next_retry_at=None),
        }
        state = {"query_tasks": tasks, "batch_runs": {key("source-run", "run"): {"max_attempts": 2}}}
        value = self.work(state, candidates=tuple("abcdefghijklm"))
        self.assertEqual(value["acquire"], {"h", "j", "k"})
        self.assertEqual(value["suppliers"], {"i"})
        # A superseded marker that no longer matches its replacement is an integrity fault, not quiet work.
        tasks["new-d"] = dict(tasks["new-d"], request_hash="other")
        with self.assertRaisesRegex(SafetyError, "SOURCE_SUPERSEDED_BINDING_INVALID"):
            self.work(state, candidates=("d",))


class NearlyReadyStagePlanTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FunctionalFitTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory, self.root, self.cid = self.fixture.memory, self.fixture.root, self.fixture.cid
        self.record = programme.prepare_programme(self.memory, "nearly-ready", concept_ids=[self.cid], as_of=NOW)

    def passing(self):
        self.fixture.accept(); self.fixture.complete()
        ensure_query_profiles(self.memory, query_fixtures.Runner(), concept_ids=[self.cid], as_of=NOW)

    def report_pool(self, state):
        self.pools = getattr(self, "pools", 0) + 1
        return programme._programme_report_pool(self.memory, self.record, as_of=NOW, state=state, functional_packets=True)

    def with_need(self, shortfall):
        return dict(deepcopy(self.record), inventory_release_need={"shortfall": shortfall, "upcoming_issue_ids": []})

    def plan(self, sequence=5, record=None, work=all_work):
        state = self.memory.snapshot(); control = self.record if record is None else record
        saved = deepcopy(state)
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("No extra history replay")), \
                patch("product_radar.nearly_ready.finishing_work", side_effect=work):
            value = programme._programme_stage_plan(state, control, {"programme_turn_sequence": sequence}, at=NOW,
                inputs=programme._signature(state, control), model_available=True, report_pool=lambda: self.report_pool(state))
        self.assertEqual(state, saved)
        return value

    def test_short_reports_finish_a_passing_type_before_discovery(self):
        self.passing()
        for record in (None, self.with_need(7)):
            value = self.plan(record=record)
            self.assertEqual(value["priority_concept_ids"], {stage: [self.cid] for stage in FINISHING})
            self.assertEqual(value["stage_order"][:4], ["acquire", "match", "crowding", "suppliers"])
            self.assertEqual(len(value["stage_order"]), len(programme.STAGES))
            self.assertEqual(value["discovery_position"], "after_prerequisites")
            self.assertEqual(value["discovery_after_stage"], "suppliers")
            self.assertTrue(value["report_supply_short"])

    def test_only_stages_with_actual_pending_work_are_promoted(self):
        self.passing()
        # The real reader: no source task is prepared yet, no question or capture.
        state = self.memory.snapshot()
        value = programme._programme_stage_plan(state, self.record, {"programme_turn_sequence": 5},
            at=NOW, inputs=programme._signature(state, self.record), model_available=True,
            report_pool=lambda: self.report_pool(state))
        # No saved crowding answer yet: the free check is worked out once, early.
        self.assertEqual(value["priority_concept_ids"], {"acquire": [self.cid], "crowding": [self.cid]})
        self.assertEqual(value["discovery_after_stage"], "crowding")
        nothing = lambda state, scope, candidates, *, as_of: {stage: set() for stage in NEARLY_READY_STAGES}
        value = self.plan(work=nothing)
        self.assertEqual(value["priority_concept_ids"], {})
        self.assertEqual(value["discovery_position"], "before")

    def test_planning_reads_its_one_state_inside_one_shared_read_scope(self):
        from product_radar import read_memo
        self.passing()
        scopes = []
        def work(state, scope, candidates, *, as_of):
            scopes.append(read_memo._ACTIVE.get())
            return all_work(state, scope, candidates, as_of=as_of)
        pools = []
        real = self.report_pool
        with patch.object(self, "report_pool", side_effect=lambda state: pools.append(read_memo._ACTIVE.get()) or real(state)):
            self.plan(work=work)
        self.assertTrue(scopes and scopes[0] is not None)
        self.assertIs(pools[0], scopes[0])  # the report pool shares the plan's answers
        self.assertIsNone(read_memo._ACTIVE.get())

    def test_enough_eligible_types_restore_the_normal_order(self):
        self.passing()
        value = self.plan(record=self.with_need(0))
        self.assertEqual(value["priority_concept_ids"], {})
        self.assertEqual(value["discovery_position"], "before")
        self.assertFalse(value["report_supply_short"])
        self.assertEqual(value["stage_order"], list(programme.STAGES[2:] + programme.STAGES[:2]))

    def test_unassessed_type_gets_its_checklist_not_finishing_work(self):
        self.fixture.accept()
        ensure_query_profiles(self.memory, query_fixtures.Runner(), concept_ids=[self.cid], as_of=NOW)
        self.assertEqual(self.plan()["priority_concept_ids"], {"fit": [self.cid]})

    def test_failing_checklist_is_not_promoted(self):
        self.passing()
        pool = self.report_pool(self.memory.snapshot())  # the real pool of this state, read before the patch
        with patch("product_radar.research_flow.current_assessment_fit", return_value={"eligible": False}), \
                patch.object(self, "report_pool", return_value=pool):
            self.assertEqual(self.plan()["priority_concept_ids"], {})

    def test_current_report_stock_is_not_promoted_but_expired_stock_is(self):
        self.passing()
        later = (timestamp(NOW) + timedelta(days=2)).isoformat()
        for until, expected in ((None, {}), (later, {}), ("2026-01-01T00:00:00+00:00",
                                                         {stage: [self.cid] for stage in FINISHING})):
            rows = [{"binding": {"concept_id": self.cid, "usable_until": until}}]
            with patch("product_radar.inventory_intake.current_intake_records", return_value=rows):
                self.assertEqual(self.plan()["priority_concept_ids"], expected, until)

    def test_report_stock_is_judged_against_the_current_report_pool(self):
        self.passing()
        pools = []
        def intake(state, record, *, pool=None):
            # The intake owner keeps only rows still eligible in the given pool.
            pools.append(pool)
            eligible = {row["concept_id"] for row in pool["eligible"]}
            return [{"binding": {"concept_id": self.cid, "usable_until": None}}] if self.cid in eligible else []
        with patch("product_radar.inventory_intake.current_intake_records", side_effect=intake):
            value = self.plan()
        # Admitted earlier, but now gated again (its crowding check is pending): finished, not skipped.
        decision = next(row for row in pools[0]["decisions"] if row["concept_id"] == self.cid)
        self.assertIn("crowding_pending", decision["codes"])
        self.assertEqual(value["priority_concept_ids"], {stage: [self.cid] for stage in FINISHING})
        self.assertEqual(self.pools, 1)  # built once per plan, from the plan's own state
        eligible = dict(pools[0], eligible=[{"concept_id": self.cid}])
        with patch("product_radar.inventory_intake.current_intake_records", side_effect=intake), \
                patch.object(self, "report_pool", return_value=eligible):
            self.assertEqual(self.plan()["priority_concept_ids"], {})

    def test_unreadable_stock_or_work_is_a_visible_wait(self):
        self.passing()
        with patch("product_radar.nearly_ready.report_stock_ids", side_effect=SafetyError("INVENTORY_BINDING_INVALID")):
            value = self.plan()
        self.assertIn({"concept_id": None, "reason": "INVENTORY_BINDING_INVALID"}, value["planning_waits"])
        self.assertEqual(value["priority_concept_ids"], {stage: [self.cid] for stage in FINISHING})
        broken = lambda *args, **kwargs: (_ for _ in ()).throw(SafetyError("SOURCE_REVIEW_TASK_BINDING_INVALID"))
        value = self.plan(work=broken)
        self.assertIn({"concept_id": None, "reason": "SOURCE_REVIEW_TASK_BINDING_INVALID"}, value["planning_waits"])
        self.assertEqual(value["priority_concept_ids"], {})
        failure = next(iter(programme.INTEGRITY_FAILURES))
        with patch("product_radar.nearly_ready.report_stock_ids", side_effect=SafetyError(failure)):
            with self.assertRaisesRegex(SafetyError, failure):
                self.plan()
        integrity = lambda *args, **kwargs: (_ for _ in ()).throw(SafetyError(failure))
        with self.assertRaisesRegex(SafetyError, failure):
            self.plan(work=integrity)

    def test_a_waiting_lane_keeps_its_wait(self):
        self.passing()
        state = self.memory.snapshot(); record = deepcopy(self.record)
        lane = digest({"stage": "acquire", "inputs": programme._signature(state, record)})
        record["deferrals"]["lane:acquire"] = {"stage": "lane", "target": "acquire", "binding": lane,
            "reason": "SOURCE_ACCESS_WAIT", "at": NOW, "next_retry_at": (timestamp(NOW) + timedelta(hours=1)).isoformat()}
        value = self.plan(record=record)
        self.assertEqual(value["priority_concept_ids"], {"match": [self.cid], "crowding": [self.cid], "suppliers": [self.cid]})
        self.assertEqual(value["stage_order"][:3], ["match", "crowding", "suppliers"])

    def test_enough_supply_and_a_waiting_fit_lane_read_no_checklist(self):
        self.passing()
        state = self.memory.snapshot(); record = self.with_need(0)
        lane = digest({"stage": "fit", "inputs": programme._signature(state, record)})
        record["deferrals"]["lane:fit"] = {"stage": "lane", "target": "fit", "binding": lane,
            "reason": "AI_RATE_LIMITED", "at": NOW, "next_retry_at": (timestamp(NOW) + timedelta(hours=1)).isoformat()}
        with patch("product_radar.research_flow.current_assessment_fit", side_effect=AssertionError("not needed")):
            self.assertEqual(self.plan(record=record)["priority_concept_ids"], {})

    def advance_four(self):
        noop = {stage: (lambda *_args: {"state": "checked"}) for stage in programme.STAGES}
        for _ in range(4):
            programme.advance_programme_turn(self.memory, self.record["programme_id"], root=self.root,
                as_of=NOW, handlers={}, steps=noop)

    def test_actual_odd_round_finishes_the_type_before_discovery_spends_slots(self):
        self.passing(); self.advance_four()
        order, seen = [], {}
        steps = {}
        for stage in programme.STAGES:
            def record_stage(memory, record, context, name=stage):
                order.append(name); seen[name] = deepcopy(context["priority_concept_ids"].get(name))
                return {"state": "checked"}
            steps[stage] = record_stage
        def discovery(memory, record, context):
            order.append("discovery")
            return {"state": "checked"}
        with patch("product_radar.nearly_ready.finishing_work", side_effect=all_work):
            value = programme.advance_programme_turn(self.memory, self.record["programme_id"], root=self.root,
                runner=FakeRunner(), as_of=NOW, handlers={}, steps=steps, discovery_step=discovery)
        self.assertEqual(order[:5], ["acquire", "match", "crowding", "suppliers", "discovery"])
        self.assertEqual({stage: seen[stage] for stage in FINISHING}, {stage: [self.cid] for stage in FINISHING})
        self.assertEqual(value["execution_order"]["discovery_after_stage"], "suppliers")

    def spend_round(self, shortfall=None):
        """Finishing work tries every call it can; discovery then asks for three."""
        self.passing(); self.advance_four()
        if shortfall is not None:
            record = programme.load_programme(self.memory, self.record["programme_id"])
            record["inventory_release_need"] = {"shortfall": shortfall, "upcoming_issue_ids": []}
            programme._save(self.memory, record)
        spent = {"finishing": 0, "discovery": 0}
        def ask(memory, context, owner, count):
            for n in range(count):
                try:
                    Jobs(memory, context["runner"]).run("A", {"items": [{"id": owner + "-" + str(n),
                        "source_text": {"title": "Synthetic " + owner + " item " + str(n)}}]})
                    spent[owner] += 1
                except SafetyError as error:
                    self.assertEqual(str(error), "WORK_TURN_MODEL_LIMIT"); break
        steps = {stage: (lambda *_args: {"state": "checked"}) for stage in programme.STAGES}
        steps["match"] = lambda memory, record, context: ask(memory, context, "finishing", 20) or {"state": "checked"}
        def discovery(memory, record, context):
            ask(memory, context, "discovery", 3)
            return {"state": "checked"}
        policy = deepcopy(DEFAULT_POLICY); policy.update(report_slots=0)
        with patch("product_radar.nearly_ready.finishing_work", side_effect=all_work):
            programme.advance_programme_turn(self.memory, self.record["programme_id"], root=self.root,
                runner=FakeRunner(), as_of=NOW, handlers={}, steps=steps, discovery_step=discovery, turn_policy=policy)
        return spent

    def test_short_round_keeps_two_calls_for_discovery(self):
        self.assertEqual(self.spend_round(), {"finishing": 8, "discovery": 2})

    def test_enough_supply_round_keeps_no_discovery_share(self):
        # Fair order: discovery runs first on this odd round and nothing is held.
        self.assertEqual(self.spend_round(shortfall=0), {"finishing": 7, "discovery": 3})

    def test_follow_on_group_reads_first_without_taking_the_parent_share(self):
        self.passing()
        other = next(cid for cid in self.memory.snapshot()["concepts"] if cid != self.cid)
        record = programme.prepare_programme(self.memory, "with-follow-on", concept_ids=[other], as_of=NOW)
        self.assertEqual(programme._register_discovered_products(self.memory, record,
            {"state": "checked", "new_concept_ids": [self.cid]}, NOW), [self.cid])
        programme._save(self.memory, record)
        self.record = record; self.advance_four()
        served = []
        steps = {stage: (lambda *_args: {"state": "checked"}) for stage in programme.STAGES}
        def acquire(memory, scope, context):
            served.append((scope["programme_id"] == record["programme_id"], context["source_limit"]))
            return {"handler_calls": context["source_limit"], "fresh_dispatches": context["source_limit"]}
        steps["acquire"] = acquire
        with patch("product_radar.nearly_ready.finishing_work", side_effect=all_work):
            value = programme.advance_programme_turn(self.memory, record["programme_id"], root=self.root,
                runner=FakeRunner(), as_of=NOW, handlers={}, steps=steps, source_limit=12)
        self.assertEqual(value["execution_order"]["priority_concept_ids"]["acquire"], [self.cid])
        # Parent keeps its capped first share; the promoted follow-on group gets
        # the rest before the parent's final pass, never the parent's quota.
        self.assertEqual(served, [(True, 6), (False, 6)])

    def test_failed_stock_refresh_drops_a_stale_enough_signal(self):
        record = self.with_need(0)
        with patch.object(programme, "refresh_reporting_intake", side_effect=SafetyError("INVENTORY_BINDING_INVALID")):
            refreshed = programme._refresh_inventory_boundary(self.memory, record, as_of=NOW)
        self.assertEqual(refreshed["inventory_preparation_wait"], "INVENTORY_BINDING_INVALID")
        self.assertNotIn("inventory_release_need", refreshed)
        self.assertTrue(report_supply_short(refreshed))

    def test_acquisition_plan_reorders_follow_on_groups_only(self):
        parent = {"programme_id": "p", "concept_ids": ["a"]}
        old, new = {"programme_id": "c1", "concept_ids": ["b"]}, {"programme_id": "c2", "concept_ids": ["c"]}
        plan = [(parent, 6), (old, None), (new, None), (parent, None)]
        self.assertEqual(programme._priority_acquisition_plan(plan, parent, {"c"}),
                         [(parent, 6), (new, None), (old, None), (parent, None)])
        self.assertEqual(programme._priority_acquisition_plan(plan, parent, {"a"}), plan)
        self.assertIs(programme._priority_acquisition_plan(plan, parent, set()), plan)
        self.assertEqual(programme._priority_acquisition_plan([(old, None), (new, None), (parent, None)], parent, {"c"}),
                         [(new, None), (old, None), (parent, None)])

    def test_stages_hand_their_priority_to_the_review_slices(self):
        self.passing()
        context = {"at": NOW, "runner": None, "supplier_limit": 5,
                   "priority_concept_ids": {"suppliers": [self.cid], "match": [self.cid]}}
        response = {"selected_capture_ids": [], "selected_inputs": [], "accepted": {}, "failures": {},
                    "reused_capture_ids": [], "remaining_inputs": 0, "source_bindings": [], "skipped_captures": []}
        with patch("product_radar.source_interpretation.review_supplier_results", return_value=response) as supplied:
            programme._suppliers(self.memory, self.record, context)
        self.assertEqual(supplied.call_args.kwargs["priority_concept_ids"], [self.cid])
        with patch("product_radar.source_interpretation.review_supplier_results", return_value=response) as supplied:
            programme._suppliers(self.memory, self.record, dict(context, priority_concept_ids={}))
        self.assertEqual(supplied.call_args.kwargs["priority_concept_ids"], [])
        with patch("product_radar.source_interpretation.review_source_results",
                   return_value={"outcomes": [], "reused": []}) as matched:
            programme._match(self.memory, self.record, dict(context, match_limit=5, retry_seconds=300))
        self.assertEqual(matched.call_args.kwargs["priority_concept_ids"], [self.cid])


class EarlySupplierObligationTests(unittest.TestCase):
    """An early supplier search follows the passing checklist that queued it."""
    def setUp(self):
        import test_inventory_intake as intake_fixtures
        self.fixture = intake_fixtures.InventoryIntakeTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory = self.fixture.memory

    def need(self, cid, **extra):
        return {"concept_id": cid, "code": "supplier_search_required", "blocking": True,
                "reason": "No accepted cursory supplier search is linked to this product yet.", **extra}

    def rows(self):
        return {row["binding"]["subject"]["concept_id"]: row for row in self.memory.snapshot()["batch_runs"].values()
                if row.get("kind") == WORK_KIND}

    def touch(self, cid):
        # A re-assessment is a committed checklist change for this subject.
        old = self.memory.snapshot()["assessments"].get(cid)
        self.memory.put("assessments", cid, {"synthetic_reassessment": cid, "after": deepcopy(old)}, previous=old)

    def test_failing_checklist_closes_only_its_early_supplier_search(self):
        pool = self.fixture.pool(needed_supplier_work=[self.need("first", checklist_hash="a" * 64), self.need("second")])
        self.fixture.refresh(pool)
        self.assertEqual(set(self.rows()), {"first", "second"})
        for cid in ("first", "second"):
            self.touch(cid)
        later = self.fixture.pool(decisions=[{"concept_id": cid, "codes": ["fit_information_pending"]}
                                             for cid in ("first", "second")])
        self.fixture.refresh(later)
        rows = self.rows()
        self.assertEqual(rows["first"]["progress"]["state"], "superseded")
        self.assertIn("no longer passes the ten-point checklist", rows["first"]["progress"]["reason"])
        self.assertNotEqual(rows["second"]["progress"]["state"], "superseded")  # Not an early-queued search.

    def test_a_still_passing_type_keeps_its_supplier_search(self):
        self.fixture.refresh(self.fixture.pool(needed_supplier_work=[self.need("first", checklist_hash="a" * 64)]))
        self.touch("first")
        self.fixture.refresh(self.fixture.pool(decisions=[{"concept_id": "first", "codes": ["supplier_search_required"]}]))
        self.assertNotEqual(self.rows()["first"]["progress"]["state"], "superseded")


class ReviewSliceOrderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = source_fixtures.SourceInterpretationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory = self.fixture.memory

    def test_priority_type_source_question_is_selected_first(self):
        for priority, expected in (((), "first"), (["second"], "second")):
            fixture = source_fixtures.SourceInterpretationTests(); fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            fixture.acquired(ids=["first", "second"]); runner = source_fixtures.Runner()
            review_source_results(fixture.memory, runner, run_id="full-list", limit=1, as_of=source_fixtures.NOW,
                                  priority_concept_ids=priority)
            self.assertEqual([item["id"] for item in runner.calls[0]["items"]], [expected])
        with self.assertRaisesRegex(SafetyError, "SOURCE_REVIEW_PRIORITY_INVALID"):
            review_source_results(self.memory, runner, run_id="full-list", priority_concept_ids="second")

    def test_priority_type_supplier_capture_is_interpreted_first(self):
        for cid in ("first", "second"):
            fixture = source_fixtures.SourceInterpretationTests(); fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            captures = {name: capture_saved_supplier_bundle(fixture.memory, {"concept_id": name, "lead": {
                "id": "fixture-lead-" + name, "urls": [SUPPLIER_BASE + "/product/caddy"]}}, fetcher=Reader())["capture_id"]
                for name in ("first", "second")}
            self.assertNotEqual(captures["first"], captures["second"])
            with patch("product_radar.suppliers.utc_now", return_value=source_fixtures.NOW):
                result = review_supplier_results(fixture.memory, FixtureRunner(), limit=1, as_of=source_fixtures.NOW,
                                                 priority_concept_ids={cid})
            self.assertEqual(result["selected_capture_ids"], [captures[cid]])
            self.assertEqual(result["remaining_inputs"], 1)
        with self.assertRaisesRegex(SafetyError, "SOURCE_REVIEW_PRIORITY_INVALID"):
            review_supplier_results(self.memory, FixtureRunner(), priority_concept_ids=[1])

class ScopedSupplierPriorityTests(unittest.TestCase):
    """The live path: a promoted accepted type's pages compete with legacy captures."""
    def setUp(self):
        import test_scoped_supplier_source_join as join_fixtures
        from test_scoped_supplier_terms import Reader as ScopedReader, BASE, NOW as SCOPED_NOW
        self.join = join_fixtures.ScopedSupplierSourceJoinTests(); self.join.setUp()
        self.addCleanup(self.join.doCleanups)
        self.memory, self.cid, self.now = self.join.memory, self.join.cid, SCOPED_NOW
        self.other = next(cid for cid in self.memory.snapshot()["concepts"] if cid != self.cid)
        self.legacy = capture_saved_supplier_bundle(self.memory, {"concept_id": self.other, "lead": {
            "id": "fictional-legacy-lead", "urls": [BASE + "/about"]}}, fetcher=ScopedReader())["capture_id"]

    def review(self, priority):
        from test_scoped_supplier_terms import CandidateRunner
        scoped, legacy = CandidateRunner(), FixtureRunner()
        class runner:
            identity, calls = scoped.identity, []
            def __call__(self, role, payload, folder):
                self.calls.append(payload)
                return (scoped if payload.get("mode") == "supplier_scope_candidates" else legacy)(role, payload, folder)
        runner = runner()
        with patch("product_radar.supplier_checks.utc_now", return_value=self.now), \
             patch("product_radar.suppliers.utc_now", return_value=self.now):
            result = review_supplier_results(self.memory, runner, as_of=self.now, limit=1,
                concept_ids=[self.cid, self.other], scoped_concept_ids=[self.cid], priority_concept_ids=priority)
        return result, runner

    def test_priority_scoped_pages_go_before_legacy_captures(self):
        result, runner = self.review({self.cid})
        self.assertEqual(result["selected_capture_ids"], [self.join.saved["capture_id"]])
        self.assertNotIn(self.legacy, result["selected_capture_ids"])
        self.assertEqual(len(runner.calls), 1)
        self.assertTrue(all(call["mode"] == "supplier_scope_candidates" for call in runner.calls))
        self.assertEqual(len(result["accepted"]), 1)

    def test_without_priority_the_legacy_capture_keeps_its_place(self):
        result, _ = self.review(())
        self.assertEqual(result["selected_capture_ids"], [self.legacy])


if __name__ == "__main__":
    unittest.main()
