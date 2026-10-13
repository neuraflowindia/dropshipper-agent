"""Typed overlays and legacy paid tasks never acquire each other's meaning."""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.models import SafetyError
from product_radar.query_dispatch import find_existing_task
from product_radar.research_memory import ResearchMemory, key
from product_radar.research_operations import bind_prepared_action, missing_market_checks, plan_research, sync_query_feedback
from product_radar.research_records import (
    CATALOGUE_SCOPE_KIND, EXAMPLE_MEMBERSHIP_KIND, FUNCTIONAL_DEFINITION_KIND,
    RESEARCH_QUESTION_KIND, SCOPED_EVIDENCE_KIND, is_paid_query_task,
    iter_catalogue_scopes, iter_example_memberships, iter_functional_definitions,
    iter_scoped_evidence,
)
from product_radar.storage import Journal
from test_research_planner import catalogue, NOW


def paid_task(**changes):
    """Actual bind_prepared_action shape, with no invented paid kind marker."""
    value = {"plan_id": "plan-original", "action_id": "action-original", "family_id": "sink-caddy",
             "attempt_id": "attempt-original", "config_hash": "a" * 64,
             "status": "prepared", "baseline_concepts": [], "collector_dispatched": False,
             "request": {"source": "meta", "route": "apify", "build": "2.7.26", "schema_version": 1,
                         "purpose": "discovery", "term": "sink caddy", "language": "en", "country": "IN",
                         "date_scope": {"kind": "active_ads_at_capture"}, "coverage": {"limit": 5}, "options": {}}}
    value.update(changes)
    return value


class TypedReadersTests(unittest.TestCase):
    def setUp(self):
        self.readers = ((FUNCTIONAL_DEFINITION_KIND, iter_functional_definitions),
                        (EXAMPLE_MEMBERSHIP_KIND, iter_example_memberships),
                        (SCOPED_EVIDENCE_KIND, iter_scoped_evidence),
                        (CATALOGUE_SCOPE_KIND, iter_catalogue_scopes))
        self.state = {"source_checks": {"legacy": {"source": "meta", "concept_id": "type-one"},
                                        "future": {"kind": "future-overlay-v9", "concept_id": "type-one"},
                                        "malformed": None}}
        for index, (kind, _) in enumerate(self.readers):
            self.state["source_checks"]["record-" + str(index)] = {"kind": kind, "concept_id": "type-one"}
            self.state["source_checks"]["other-" + str(index)] = {"kind": kind, "concept_id": "type-two"}

    def test_each_reader_selects_only_its_explicit_kind_and_original_id(self):
        before = deepcopy(self.state)
        for index, (kind, reader) in enumerate(self.readers):
            self.assertEqual(list(reader(self.state, "type-one")),
                             [("record-" + str(index), {"kind": kind, "concept_id": "type-one"})])
            self.assertEqual(len(list(reader(self.state))), 2)
            self.assertEqual(list(reader(self.state, "unknown-type")), [])
        self.assertEqual(self.state, before)

    def test_empty_legacy_state_is_readable(self):
        for _, reader in self.readers:
            self.assertEqual(list(reader({})), [])

    def test_scoped_evidence_concept_subject_is_not_a_fake_direction_id(self):
        concept = {"kind": SCOPED_EVIDENCE_KIND,
                   "subject": {"kind": "concept", "concept_id": "type-three", "definition_hash": "b" * 64}}
        direction = {"kind": SCOPED_EVIDENCE_KIND, "concept_id": "type-three",
                     "subject": {"kind": "catalogue_direction", "family_id": "type-three"}}
        state = {"source_checks": {"concept": concept, "direction": direction}}
        self.assertEqual(list(iter_scoped_evidence(state, "type-three")), [("concept", concept)])
        self.assertEqual(len(list(iter_scoped_evidence(state))), 2)


class PaidTaskRecognitionTests(unittest.TestCase):
    def test_original_preparation_and_feedback_statuses_remain_recognised(self):
        for status in ("prepared", "pending", "running", "completed", "partial", "failed"):
            with self.subTest(status=status):
                task = paid_task(status=status)
                before = deepcopy(task)
                self.assertTrue(is_paid_query_task(task))
                self.assertEqual(task, before)

    def test_older_recovery_fixture_without_action_id_remains_recognised(self):
        task = paid_task()
        del task["action_id"]
        self.assertTrue(is_paid_query_task(task))

    def test_every_explicit_other_kind_is_not_paid_even_with_matching_fields(self):
        for kind in ("all_source_task_v1", RESEARCH_QUESTION_KIND, "unknown-v99", "paid-looking-v1", None):
            with self.subTest(kind=kind):
                self.assertFalse(is_paid_query_task(paid_task(kind=kind)))

    def test_missing_original_ownership_or_bounds_is_not_a_prepared_paid_task(self):
        for field in ("plan_id", "family_id", "attempt_id", "config_hash", "status",
                      "baseline_concepts", "collector_dispatched", "request"):
            with self.subTest(field=field):
                task = paid_task()
                del task[field]
                self.assertFalse(is_paid_query_task(task))
        for field in paid_task()["request"]:
            with self.subTest(request_field=field):
                task = paid_task()
                del task["request"][field]
                self.assertFalse(is_paid_query_task(task))

    def test_malformed_values_do_not_raise_or_authorise_paid_work(self):
        for changes in ({"status": []}, {"status": "dispatching"}, {"attempt_id": None},
                        {"config_hash": "g" * 64}, {"collector_dispatched": 1},
                        {"baseline_concepts": [None]}, {"baseline_concepts": {}}, {"request": None}):
            with self.subTest(changes=changes):
                self.assertFalse(is_paid_query_task(paid_task(**changes)))
        for record in (None, [], "paid", 3):
            self.assertFalse(is_paid_query_task(record))

    def test_browser_request_is_not_paid_and_future_metadata_does_not_erase_paid_owner(self):
        task = paid_task()
        task["request"]["route"] = "browser"
        self.assertFalse(is_paid_query_task(task))
        task = paid_task(acquisition_id="saved-original-dataset", refinement={"kind": "empty_result_alternative"})
        self.assertTrue(is_paid_query_task(task))


class MixedConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.memory = ResearchMemory(root / "research", Journal(root / "finance"), evidence_type="synthetic")

    def test_unknown_and_v2_tasks_cannot_hide_original_paid_preparation(self):
        task = paid_task()
        for task_id, kind in (("a-unknown", "unknown-v99"), ("b-question", RESEARCH_QUESTION_KIND),
                              ("c-source", "all_source_task_v1")):
            self.memory.put("query_tasks", task_id, paid_task(kind=kind))
        self.memory.put("query_tasks", "z-paid-original", task)
        before = self.memory.snapshot()
        with patch.object(self.memory.journal, "state", return_value=({}, {})):
            result = find_existing_task(self.memory, {"family_id": "sink-caddy", "request": task["request"]}, as_of=NOW)
        self.assertEqual(result["task_id"], "z-paid-original")
        self.assertEqual(result["reuse_kind"], "active_task")
        self.assertEqual(self.memory.snapshot(), before)

    def test_v2_without_paid_fields_does_not_crash_feedback_or_count_as_paid(self):
        self.memory.put("query_tasks", "v2-question", {"kind": RESEARCH_QUESTION_KIND,
                        "subject": {"kind": "catalogue_direction", "family_id": "sink-caddy"}, "status": "ready"})
        before = self.memory.snapshot()
        self.assertEqual(sync_query_feedback(self.memory, as_of=NOW), [])
        self.assertEqual(self.memory.snapshot(), before)

    def test_typed_overlays_do_not_become_global_route_votes_or_break_plan_view(self):
        for index, kind in enumerate((FUNCTIONAL_DEFINITION_KIND, EXAMPLE_MEMBERSHIP_KIND,
                                      SCOPED_EVIDENCE_KIND, CATALOGUE_SCOPE_KIND, "future-overlay-v9")):
            self.memory.put("source_checks", "overlay-" + str(index),
                            {"kind": kind, "concept_id": "type-one", "source": "meta", "at": NOW,
                             "state": "blocked", "reason": "Not a provider-route observation."})
        self.assertEqual(missing_market_checks({"products": []}, as_of=NOW,
                         source_checks=self.memory.snapshot()["source_checks"].values()), [])
        plan = plan_research(self.memory, catalogue(), as_of=NOW, slots=1)
        self.assertEqual(plan["source_checks"], [])
        self.assertEqual(len(plan["actions"]), 1)
        self.assertFalse(plan["collector_dispatched"])

    def test_planner_explicit_refinement_input_rejects_nonpaid_contract(self):
        from product_radar.research_planner import plan_searches
        with self.assertRaisesRegex(SafetyError, "PLANNER_REFINEMENT_TASK_INVALID"):
            plan_searches(catalogue(), [], as_of=NOW, refinement_tasks=[paid_task(kind=RESEARCH_QUESTION_KIND)])

    def test_prepared_binding_cannot_reuse_or_overwrite_a_different_contract(self):
        plan = plan_research(self.memory, catalogue(), as_of=NOW, slots=1)
        action = plan["actions"][0]
        request = action["request"]
        prepared = SimpleNamespace(build=request["build"], purpose=request["purpose"],
                    attempt_id="attempt-original", config_hash="a" * 64,
                    input={"urls": [{"url": "https://www.facebook.com/ads/library/?" +
                                      urlencode({"q": action["phrase"], "country": "IN"})}],
                           "limitPerSource": request["coverage"]["limit"]})
        task_id = key("task", [plan["id"], action["action_id"]])
        self.memory.put("query_tasks", task_id, paid_task(kind=RESEARCH_QUESTION_KIND))
        before = self.memory.snapshot()
        with self.assertRaisesRegex(SafetyError, "RESEARCH_TASK_ALREADY_BOUND"):
            bind_prepared_action(self.memory, plan["id"], action["action_id"], prepared)
        self.assertEqual(self.memory.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
