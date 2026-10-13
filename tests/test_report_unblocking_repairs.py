"""Report-unblocking repairs: ordinals, current-format fallback and intake,
review ordering, photo allowance and the protected review share.

Synthetic records and fixtures only; no source, provider, model or portal call.
"""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import product_radar.issue_allocation as allocation
import product_radar.programme as programme
from product_radar.inventory_intake import (INTAKE_KIND, eligibility_signal, refresh_inventory_intake,
                                            reporting_intake_concept_ids)
from product_radar.models import digest, timestamp
from product_radar.report_examples import RECOMMENDATION_CONTRACT
from product_radar.report_selection import build_report_pool
from product_radar.research_ai import Jobs
from product_radar.research_memory import TABLES
from product_radar.work_turns import (DEFAULT_POLICY, active_work_turn, bind_work_turn, close_work_turn,
                                      open_work_turn, reserve_stage_items)
from test_issue_allocation import DATES, products
import test_programme as fixtures

SLOTS = [{"issue_id": "issue-default-" + day, "client_id": "default", "date": day,
          "scheduled_at": day + "T03:30:00+00:00"} for day in DATES]


def legacy_issue(slot, *, ready):
    return dict(slot, revision=1, products=[], ready_for_review=ready, shortfall=0 if ready else 10,
                input_hash="synthetic-" + slot["issue_id"])


class ReportOrdinalTests(unittest.TestCase):
    def test_frozen_set_after_an_unfilled_date_keeps_its_first_position(self):
        fresh = allocation._fresh_at_release
        with patch.object(allocation, "_fresh_at_release",
                          side_effect=lambda p, i, **o: i["date"] != DATES[0] and fresh(p, i, **o)):
            issue = allocation.allocate_issues(products(10), DATES, [], [])["issues"][1]
        self.assertEqual((issue["ready_for_review"], issue["projected_ordinal"]), (True, 1))
        state = {table: {} for table in TABLES}
        record = {"release_slots": SLOTS}
        eligible = {row["concept_id"]: row for row in products(10)}
        self.assertIsNone(programme._current_report_selection(issue, eligible, state, record))
        # A real produced set elsewhere still moves it, exactly as before.
        state["issue_reservations"]["earlier"] = {"issue_id": "earlier", "client_id": "default", "date": "2026-10-10",
                                                  "state": "reserved", "products": []}
        self.assertEqual(programme._current_report_selection(issue, eligible, state, record),
                         "REPORT_LANGUAGE_ORDINAL_CHANGED")


class ReportFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        state = {table: {} for table in TABLES}
        self.memory = SimpleNamespace(root=self.root, snapshot=lambda: deepcopy(state), evidence_type="synthetic")
        self.record = {"programme_id": "current-week", "client_id": "default", "release_slots": deepcopy(SLOTS),
                       "drafts": {}, "deferrals": {}, "cycles": 0}
        self.legacy_row = {"concept_id": "older-format-row"}
        self.current_row = {"concept_id": "current-format-row", "recommendation_contract": RECOMMENDATION_CONTRACT}

    def run_reports(self, *, cooperative, preview, fallback, frozen=None):
        seen, revised = [], []
        def allocate(available, pending, *_args, **_kwargs):
            seen.append(([row["concept_id"] for row in available], [slot["issue_id"] for slot in pending]))
            return {"issues": deepcopy(fallback)}
        def revision(root, issue, facts, evidence_type):
            revised.append(deepcopy(issue)); return issue
        context = {"root": self.root, "at": "2026-10-13T16:00:00+00:00", "retry_seconds": 300, "runner": None}
        if cooperative:
            context["cooperative_mode"] = True
        with patch.object(programme, "_programme_report_pool", return_value={"facts_by_concept": {}}), \
                patch.object(programme, "_reporting_eligible", return_value=[self.legacy_row, self.current_row]), \
                patch.object(programme, "_prepare_reporting_examples", return_value={"attempted": 0, "prepared": [], "waiting": []}), \
                patch.object(programme, "_frozen_report_input",
                             side_effect=lambda _root, slot, *_a: (frozen or {}).get(slot["issue_id"])), \
                patch.object(programme, "_current_report_selection", return_value="REPORT_CURRENT_ELIGIBILITY_CHANGED"), \
                patch.object(programme, "_functional_report_allocations", return_value=deepcopy(preview)), \
                patch.object(programme, "_functional_report_revision", side_effect=lambda root, issue, facts, *a, **k: (issue, facts)), \
                patch.object(programme, "_report_revision", side_effect=revision), \
                patch("product_radar.issue_allocation.allocate_issues", side_effect=allocate), \
                patch("product_radar.issue_allocation.reserve_issue", side_effect=lambda _m, issue, **_k: issue), \
                patch("product_radar.report_workflow.freeze_review_input"), \
                patch("product_radar.report_workflow.prepare_review_package",
                      side_effect=lambda _m, issue, *_a, **_k: {"issue_id": issue["issue_id"], "state": "waiting",
                                                               "reason": "SYNTHETIC", "complete": False}):
            programme._reports(self.memory, self.record, context)
        return seen, revised

    def test_cooperative_fallback_never_refills_new_dates_with_older_format_rows(self):
        seen, _ = self.run_reports(cooperative=True, preview=[], fallback=[])
        self.assertEqual(seen, [(["current-format-row"], [slot["issue_id"] for slot in SLOTS[1:]])])
        legacy, _ = self.run_reports(cooperative=False, preview=[], fallback=[])
        self.assertEqual(legacy[0][0], ["older-format-row", "current-format-row"])

    def test_explicit_turn_starts_no_new_set_after_its_release_time(self):
        seen, _ = self.run_reports(cooperative=True, preview=[], fallback=[])
        self.assertEqual(seen[0][1], [SLOTS[1]["issue_id"], SLOTS[2]["issue_id"]])  # 10-13 09:00 IST has passed.
        self.assertEqual(self.record["drafts"][SLOTS[0]["issue_id"]]["state"], "release_passed")
        self.assertEqual(self.record["drafts"][SLOTS[0]["issue_id"]]["reason"], "REPORT_RELEASE_TIME_PASSED")
        # An existing set for that past date may still receive its revision.
        existing = {SLOTS[0]["issue_id"]: {"issue": dict(legacy_issue(SLOTS[0], ready=True)), "product_facts": None}}
        seen, _ = self.run_reports(cooperative=True, preview=[], fallback=[], frozen=existing)
        self.assertEqual(seen[0][1], [slot["issue_id"] for slot in SLOTS])

    def test_fallback_ordinal_counts_produced_sets_not_unfilled_dates(self):
        functional = dict(legacy_issue(SLOTS[1], ready=True), recommendation_contract=RECOMMENDATION_CONTRACT,
                          projected_ordinal=1, language_state="bilingual_trial")
        fallback = [legacy_issue(SLOTS[0], ready=False), legacy_issue(SLOTS[2], ready=True)]
        seen, revised = self.run_reports(cooperative=False, preview=[functional], fallback=fallback)
        self.assertEqual(seen[0][1], [SLOTS[0]["issue_id"], SLOTS[2]["issue_id"]])
        ordinals = {issue["issue_id"]: (issue["projected_ordinal"], issue["language_state"]) for issue in revised}
        # The unfilled first date neither pushes the third date past the
        # bilingual trial nor occupies a position itself.
        self.assertEqual(ordinals[SLOTS[2]["issue_id"]], (2, "bilingual_trial"))


class DefinitionReviewOrderTests(unittest.TestCase):
    def state(self):
        state = {table: {} for table in TABLES}
        state["supplier_assessments"]["lead"] = {"input": {"product": {"concept_id": "c3"}}}
        state["supplier_assessments"]["other"] = {"input": "not-a-product"}
        for n, (cid, outcome) in enumerate((("c2", "completed_relevant"), ("c2", "completed_relevant"),
                                            ("c4", "completed_relevant"), ("c1", "completed_no_results"))):
            state["query_tasks"]["task" + str(n)] = {"target_results": {cid: {"state": outcome}}}
        return state

    def test_closest_to_report_eligibility_is_reviewed_first(self):
        eligible = lambda state, cid, **_options: {"eligible": cid == "c4"}
        with patch("product_radar.research_flow.current_assessment_fit", side_effect=eligible):
            order = programme._definition_review_order(self.state(), ["c0", "c1", "c2", "c3", "c4"], defined={"c0"})
        self.assertEqual(order, ["c4", "c3", "c2", "c1", "c0"])

    def test_older_format_report_selections_come_first_in_any_hold_state(self):
        state = self.state()
        state["issue_reservations"]["older"] = {"state": "cancelled", "products": [{"concept_id": "c1"}]}
        state["issue_reservations"]["current"] = {"state": "reserved", "recommendation_contract": RECOMMENDATION_CONTRACT,
                                                  "products": [{"concept_id": "c2"}]}
        state["issue_reservations"]["bad"] = {"state": "reserved", "products": ["not-a-product"]}
        with patch("product_radar.research_flow.current_assessment_fit", return_value=None):
            order = programme._definition_review_order(state, ["c4", "c3", "c2", "c1"])
        self.assertEqual(order, ["c1", "c3", "c2", "c4"])

    def test_short_or_malformed_inputs_keep_deterministic_order(self):
        self.assertEqual(programme._definition_review_order(self.state(), ["c2", "c1"], defined={"c1"}), ["c2", "c1"])
        broken = self.state(); broken["associations"]["bad"] = {"semantic_id": "missing-concept-key"}
        with patch("product_radar.research_flow.current_assessment_fit", side_effect=AssertionError("not reached")):
            order = programme._definition_review_order(broken, ["c4", "c3", "c2", "c1"])
        self.assertEqual(order, ["c3", "c2", "c4", "c1"])
        with patch("product_radar.research_flow.current_assessment_fit", side_effect=KeyError("fixture")):
            self.assertEqual(programme._definition_review_order(self.state(), ["c2", "c1"]), ["c2", "c1"])


class _ProgrammeFixture(unittest.TestCase):
    """Borrow the ordinary synthetic programme fixture without its tests."""
    def setUp(self):
        fixture = fixtures.ProgrammeTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        self.root, self.memory, self.now = fixture.root, fixture.memory, fixture.now
        self.prepare, self.noops = fixture.prepare, fixture.noops


class PhotoAllowanceTests(_ProgrammeFixture):
    def run_photos(self, *, spent):
        record = self.prepare()
        candidates = {cid: {"source_url": "https://images.example.org/" + cid + ".png", "relationship": "exact_product"}
                      for cid in ("first", "second")}
        policy = deepcopy(DEFAULT_POLICY); policy["purpose_items"]["photos"] = 1
        turn = open_work_turn(self.memory, record["programme_id"], at=self.now, policy=policy)
        calls = []
        def readiness(_memory, cid, _candidate, **_options):
            return {"ready": True, "local_saved": cid == "second", "next_retry_at": None, "reason": None}
        def capture(_memory, cid, _candidate, **_options):
            calls.append(cid)
            return {"state": "failed", "reason": "SYNTHETIC_NOT_USABLE", "reference": "ref-" + cid}
        context = {"at": self.now, "photo_limit": 10, "retry_seconds": 300}
        with bind_work_turn(self.memory, turn["id"]), \
                patch.object(programme, "_programme_report_pool",
                             return_value={"facts_by_concept": {cid: {"image_candidates": [row]} for cid, row in candidates.items()}}), \
                patch("product_radar.product_images.current_public_image", return_value=None), \
                patch("product_radar.product_images.product_image_readiness", side_effect=readiness), \
                patch("product_radar.product_images.capture_product_image", side_effect=capture):
            if spent:
                reserve_stage_items(self.memory, "photos", "earlier-photo", 1, at=self.now)
            result = programme._photos(self.memory, record, context)
        return calls, result

    def test_spent_photo_allowance_skips_fresh_reads_but_adopts_saved_originals(self):
        calls, result = self.run_photos(spent=True)
        self.assertEqual(calls, ["second"])
        self.assertEqual(result["outcomes"][0], {"concept_id": "first", "state": "waiting",
                                                 "reason": "WORK_TURN_PURPOSE_LIMIT", "references": []})
        self.assertEqual([row["concept_id"] for row in result["outcomes"]], ["first", "second"])

    def test_open_photo_allowance_attempts_every_ready_product(self):
        calls, result = self.run_photos(spent=False)
        self.assertEqual(calls, ["first", "second"])
        self.assertNotIn("waiting", [row["state"] for row in result["outcomes"]])


class ReviewShareTurnTests(_ProgrammeFixture):
    def ordinary(self, turn, item_id):
        from test_work_turns import Runner
        with bind_work_turn(self.memory, turn["id"]):
            Jobs(self.memory, Runner()).run("A", {"items": [{"id": item_id, "source_text": {"title": "Synthetic " + item_id}}]})

    def continuation(self, record, *, semantic=False, review=False):
        from test_work_turns import Runner
        return programme._closed_model_allowance_continuation(self.memory, self.memory.snapshot(), record,
            runner=Runner(), at=self.now, retry_seconds=300, model_wait=False,
            semantic_hints=[{"model_ready": semantic}], review_hints=[{"model_ready": review}], report_inputs=[],
            source_questions={}, update_questions=[])

    def test_closed_turn_witness_separates_review_share_from_other_B_work(self):
        record = self.prepare(concept_ids=[])
        policy = deepcopy(DEFAULT_POLICY); policy.update(model_executions=2, report_slots=0)
        turn = open_work_turn(self.memory, record["programme_id"], at=self.now, policy=policy, review_ready=True)
        self.ordinary(turn, "ordinary-1")
        close_work_turn(self.memory, turn["id"], at=self.now, reason="Synthetic ordinary share spent.")
        # Ordinary B work was blocked by the protected share; a review was not.
        self.assertEqual(self.continuation(record, semantic=True)["reason"], "WORK_TURN_MODEL_LIMIT")
        self.assertIsNone(self.continuation(record, review=True))

    def test_closed_turn_witness_for_review_work_after_whole_allowance_spent(self):
        record = self.prepare(concept_ids=[])
        policy = deepcopy(DEFAULT_POLICY); policy.update(model_executions=2, report_slots=0)
        turn = open_work_turn(self.memory, record["programme_id"], at=self.now, policy=policy)
        self.ordinary(turn, "ordinary-1"); self.ordinary(turn, "ordinary-2")
        close_work_turn(self.memory, turn["id"], at=self.now, reason="Synthetic whole allowance spent.")
        witness = self.continuation(record, review=True)
        self.assertEqual((witness["purpose"], witness["reason"]), ("B", "WORK_TURN_MODEL_LIMIT"))

    def test_turn_records_review_flag_from_readiness(self):
        self.prepare()
        result = programme.advance_programme_turn(self.memory, "whole-programme", root=self.root, handlers={},
            steps=self.noops(), as_of=self.now, review_ready=True)
        self.assertIs(self.memory.snapshot()["batch_runs"][result["work_turn"]["id"]]["review_ready"], True)
        real = programme.programme_turn_readiness
        with patch.object(programme, "programme_turn_readiness",
                          side_effect=lambda *a, **k: dict(real(*a, **k), ready=True, review_ready=True)):
            ready = programme.advance_ready_programme_turn(self.memory, root=self.root,
                programme_id="whole-programme", handlers={}, steps=self.noops(), as_of=self.now)
        self.assertTrue(ready["round_completed"])
        self.assertIs(self.memory.snapshot()["batch_runs"][ready["work_turn"]["id"]]["review_ready"], True)


class FunctionalMarketCheckReviewTests(unittest.TestCase):
    """A due functional market check protects, and is protected by, the review share."""

    def setUp(self):
        from test_functional_source_market import NOW, functional_pool_fixture
        from product_radar.source_interpretation import functional_source_questions
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root, self.now = Path(self.temp.name), NOW
        self.memory, self.ids = functional_pool_fixture(self.root / "inputs", with_market=False)
        self.questions = functional_source_questions(self.memory.snapshot(), as_of=NOW)
        self.assertEqual([row["kind"] for row in self.questions], ["functional_source_scope"])

    def runner(self):
        from test_work_turns import Runner
        return Runner()

    def spend(self, record, *, ordinary, review_ready):
        policy = deepcopy(DEFAULT_POLICY); policy.update(model_executions=2, report_slots=0)
        turn = open_work_turn(self.memory, record["programme_id"], at=self.now, policy=policy, review_ready=review_ready)
        with bind_work_turn(self.memory, turn["id"]):
            for n in range(ordinary):
                item = "ordinary-" + turn["id"][-8:] + "-" + str(n)  # A saved answer would not spend.
                Jobs(self.memory, self.runner()).run("A", {"items": [{"id": item, "source_text": {"title": item}}]})
        close_work_turn(self.memory, turn["id"], at=self.now, reason="Synthetic allowance closed.")

    def continuation(self, record):
        return programme._closed_model_allowance_continuation(self.memory, self.memory.snapshot(), record,
            runner=self.runner(), at=self.now, retry_seconds=300, model_wait=False, semantic_hints=[],
            review_hints=[], report_inputs=[], source_questions={record["programme_id"]: self.questions},
            update_questions=[])

    def test_due_functional_market_check_sets_review_ready_only_with_a_runner(self):
        record = programme.prepare_programme(self.memory, "market-check", concept_ids=self.ids, as_of=self.now)
        # The fixture's saved cohort has no programme source run; serve it as this run's question.
        with patch("product_radar.source_interpretation._questions", side_effect=lambda *_a, **_k: deepcopy(self.questions)):
            ready = programme.programme_turn_readiness(self.memory, record, root=self.root, runner=self.runner(), as_of=self.now)
            idle = programme.programme_turn_readiness(self.memory, record, root=self.root, as_of=self.now)
            record["deferrals"]["match:" + self.questions[0]["task_id"] + ":" + self.questions[0]["concept_id"]] = {
                "stage": "match", "target": "deferred", "binding": digest(self.questions[0]), "reason": "SYNTHETIC_WAIT",
                "at": self.now, "next_retry_at": (timestamp(self.now) + timedelta(hours=1)).isoformat()}
            waiting = programme.programme_turn_readiness(self.memory, record, root=self.root, runner=self.runner(), as_of=self.now)
        self.assertEqual((ready["review_ready"], idle["review_ready"], waiting["review_ready"]), (True, False, False))

    def test_closed_turn_witness_for_functional_market_check(self):
        record = programme.prepare_programme(self.memory, "market-check-spent", concept_ids=[], as_of=self.now)
        self.spend(record, ordinary=1, review_ready=True)
        self.assertIsNone(self.continuation(record))  # Its protected execution was still free.
        self.spend(record, ordinary=2, review_ready=False)
        witness = self.continuation(record)
        self.assertEqual((witness["purpose"], witness["reason"]), ("E:source_support", "WORK_TURN_MODEL_LIMIT"))


class CurrentFormatIntakeTests(unittest.TestCase):
    def setUp(self):
        from test_functional_source_market import NOW, functional_pool_fixture
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.now = NOW
        self.memory, (self.cid,) = functional_pool_fixture(Path(self.temp.name) / "inputs")
        self.record = programme.prepare_programme(self.memory, "current-week", concept_ids=[], as_of=NOW)

    def pool(self, at):
        return build_report_pool(self.memory, as_of=at, recommendation_contract=RECOMMENDATION_CONTRACT)

    def refresh(self, at, pool=None, **options):
        result = refresh_inventory_intake(self.memory, self.record, as_of=at, pool=pool or self.pool(at), **options)
        self.record.update({field: result[field] for field in ("reporting_intake_ids", "inventory_work_ids")})
        return result

    def test_current_format_type_reaches_the_current_week_at_a_later_pool_time(self):
        first = self.refresh(self.now, subject_limit=1, event_limit=1)
        self.assertTrue(first["backlog_remaining"])  # The change cursor is still far behind.
        self.assertEqual(reporting_intake_concept_ids(self.memory.snapshot(), self.record), {self.cid})
        later = (timestamp(self.now) + timedelta(minutes=5)).isoformat()
        earlier_row, later_row = self.pool(self.now)["eligible"][0], self.pool(later)["eligible"][0]
        self.assertNotEqual(digest(earlier_row), digest(later_row))  # Proofs carry the pool time.
        self.assertEqual(eligibility_signal(earlier_row), eligibility_signal(later_row))
        state = self.memory.snapshot()
        self.assertEqual([row["concept_id"] for row in programme._reporting_eligible(self.record, state, self.pool(later))],
                         [self.cid])
        before = self.memory.snapshot_with_provenance()["event_count"]
        self.refresh(later, pool=self.pool(later), subject_limit=1, event_limit=1)
        intakes = [row for row in self.memory.snapshot()["batch_runs"].values() if row.get("kind") == INTAKE_KIND]
        self.assertEqual(len(intakes), 1)  # Unchanged membership is not rewritten.
        self.assertLessEqual(self.memory.snapshot_with_provenance()["event_count"] - before, 1)

    def test_changed_binding_keeps_one_newest_intake_per_type(self):
        pool = self.pool(self.now)
        self.refresh(self.now, pool=pool)
        changed = deepcopy(pool); changed["eligible"][0]["source_revisions"] = {"observations": {"newer": "1" * 64}}
        later = (timestamp(self.now) + timedelta(minutes=5)).isoformat()
        self.refresh(later, pool=changed)
        saved = self.memory.snapshot()["batch_runs"]
        intakes = [ref for ref, row in saved.items() if row.get("kind") == INTAKE_KIND]
        self.assertEqual(len(intakes), 2)
        self.assertEqual(len(self.record["reporting_intake_ids"]), 1)
        self.assertEqual(saved[self.record["reporting_intake_ids"][0]]["admitted_at"], later)

    def test_status_change_invalidates_membership(self):
        pool = self.pool(self.now)
        self.refresh(self.now, pool=pool)
        updated = deepcopy(pool); updated["eligible"][0]["status"] = "Updated"
        self.assertEqual(programme._reporting_eligible(self.record, self.memory.snapshot(), updated), [])

    def test_inventory_boundary_counts_only_current_format_stock(self):
        older = {"concept_id": "older-format", "status": "New"}
        current = {"concept_id": self.cid, "status": "New", "recommendation_contract": RECOMMENDATION_CONTRACT}
        pools = []
        with patch.object(programme, "refresh_reporting_intake",
                          return_value={"pool": {"eligible": [older, current], "needed_market_work": []}}), \
                patch.object(programme, "admit_due_inventory_work",
                             side_effect=lambda _memory, _record, **options: pools.append(options["pool"])):
            programme._refresh_inventory_boundary(self.memory, self.record, as_of=self.now)
        self.assertEqual([row["concept_id"] for row in pools[0]["eligible"]], [self.cid])
        self.assertEqual(pools[0]["needed_market_work"], [])


if __name__ == "__main__":
    unittest.main()
