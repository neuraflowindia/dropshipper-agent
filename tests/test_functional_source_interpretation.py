"""Joined accepted-type source E fixtures; no external calls or real AI."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from product_radar.functional_types import accept_functional_definition, effective_functional_definition
from product_radar.market_validation import functional_market_context
from product_radar.models import SafetyError, digest
from product_radar.research_memory import import_capture
from product_radar.research_sources import map_research_rows
from product_radar.research_scope import validated_scoped_evidence
from product_radar.source_coverage import execute_source_run, prepare_source_tasks, source_coverage
from product_radar.marketplace_browser import build_marketplace_request
from product_radar.source_interpretation import (FUNCTIONAL_MATCH_METHOD, functional_source_questions,
    review_source_results)
from test_functional_queries import DEFINITION
from test_research_sources import amazon_row, request
import test_source_interpretation as legacy_fixtures
from test_source_interpretation import NOW


LATER = "2026-10-09T10:00:00+00:00"


class Runner:
    identity = "synthetic-functional-source-v5"

    def __init__(self, *, relationship="relevant_family", scope="narrow_type", judgment=None, mutate=None):
        # judgment: the retired per-site opinion; the v5 relevance answer has none (kept for callers).
        self.calls = []
        self.relationship, self.scope, self.mutate = relationship, scope, mutate

    def __call__(self, role, payload, folder):
        self.calls.append(deepcopy(payload))
        values = []
        for item in payload["items"]:
            values.append({"id": item["id"], "criteria": [], "compliance_note": "", "missing_questions": [],
                "relevance": [{"id": alias, "relationship": self.relationship, "scope": self.scope,
                    "reason": "Synthetic explicit scope comparison.", "limitations": ["This is observed list context, not sales."]}
                    for alias in item["reference_aliases"]]})
        if self.mutate:
            action, self.mutate = self.mutate, None
            action()
        return {"results": values}, {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1}


class FunctionalSourceInterpretationTests(unittest.TestCase):
    def setUp(self):
        legacy_fixtures.SourceInterpretationTests.setUp(self)
        actual_request = build_marketplace_request("amazon-bestsellers", department="kitchen")
        planned = prepare_source_tasks(self.memory, [{"route": "amazon-bestsellers", "request": actual_request,
            "concept_ids": ["first"]}], run_id="full-list", as_of=NOW)
        def saved_cards(context):
            context["record_dispatch"]()
            values = [dict(amazon_row(), title=f"Sink caddy holder {n}", asin=f"B01234567{n}",
                link=f"https://www.amazon.in/dp/B01234567{n}") for n in range(3)]
            imported = import_capture(self.memory, source="amazon", acquisition_id=context["acquisition_id"],
                observed_at=NOW, mapped=map_research_rows(values, request()), capture_hash=digest(values),
                reference={"fixture": "synthetic", "route": "amazon-bestsellers", "request": actual_request})
            ids = sorted(ref for ref, row in self.memory.snapshot()["observations"].items()
                         if row["capture_id"] == imported["capture_id"])
            return {"schema_version": 1, "route": "amazon-bestsellers", "acquisition_id": context["acquisition_id"],
                "observed_at": NOW, "state": "usable", "dispatch_started": True,
                "reference": context["acquisition_id"] + "/receipt.json", "capture_ids": [imported["capture_id"]],
                "observation_ids": ids, "target_results": [], "reason": "Synthetic original cards.", "retryable": False}
        execute_source_run(self.memory, "full-list", handlers={"amazon-bestsellers": saved_cards}, as_of=NOW,
                           bounded_call=lambda callback, context, timeout: callback(context))
        self.task_id = planned["task_ids"][0]
        self.refs = sorted(self.memory.snapshot()["observations"])
        state = self.memory.snapshot()
        self.memory.put("associations", "real-child", {"concept_id": "first", "status": "resolved",
            "semantic_id": state["observations"][self.refs[0]]["semantic_id"]})
        self.accept()

    def accept(self, definition=None, *, at=NOW):
        return accept_functional_definition(self.memory, "first", definition or DEFINITION,
            basis_refs=[self.refs[0]], reviewer="synthetic", reviewed_at=at,
            expected_meaning_hash=effective_functional_definition(self.memory.snapshot(), "first")["meaning_hash"])

    def review(self, runner=None, **kwargs):
        return review_source_results(self.memory, runner or Runner(), run_id="full-list", as_of=NOW, **kwargs)

    def test_actual_v4_producer_preserves_tasks_captures_and_original_dates(self):
        before = self.memory.snapshot(); runner = Runner()
        result = self.review(runner)
        after = self.memory.snapshot()
        self.assertEqual(result["remaining_questions"], 0, result)
        for table in ("query_tasks", "captures", "observations", "assessments", "market_reviews"):
            self.assertEqual(after[table], before[table], table)
        item = runner.calls[0]["items"][0]
        self.assertEqual(item["meaning_hash"], effective_functional_definition(after, "first")["meaning_hash"])
        self.assertNotIn("report_title", item["description"])
        scoped = validated_scoped_evidence(after, "first", as_of=NOW)
        self.assertEqual(len(scoped), 3)  # one match row per record; no per-site opinion row (W7)
        self.assertTrue(all(row["method"] == FUNCTIONAL_MATCH_METHOD for row in scoped))
        proof = scoped[0]["job_proof"]
        self.assertEqual((proof["prompt_version"], proof["accepted_item_hash"]),
                         ("research-e-source-scope-v5", digest(proof["accepted_item"])))
        self.assertNotIn("source_support", proof["accepted_item"])
        self.assertNotIn("source_support", runner.calls[0])
        self.assertEqual({ctx["observed_at"] for row in scoped for ctx in row["source_contexts"]}, {NOW})
        calls = len(runner.calls)
        self.review(runner)
        self.assertEqual(len(runner.calls), calls)

    def test_positive_first_slice_does_not_drop_retained_unreviewed_rows(self):
        runner = Runner(judgment="reasonably_positive")
        first = self.review(runner, per_product=1)
        self.assertEqual(first["remaining_questions"], 1)
        panel = next(row for row in source_coverage(self.memory, concept_ids=["first"], as_of=NOW)["rows"]
                     if row["route"] == "amazon-bestsellers")
        self.assertEqual(panel["state"], "completed_relevant")
        self.assertEqual((panel["reviewed_observations"], panel["remaining_observations"]), (1, 2))
        self.assertEqual(panel["interpretation_state"], "pending")
        self.review(runner, per_product=1)
        final = self.review(runner, per_product=1)
        self.assertEqual(final["remaining_questions"], 0)
        self.assertEqual(len(runner.calls), 3)

    def test_neutral_unclear_and_unrelated_complete_without_positive_repeat(self):
        for relationship in ("relevant_family", "unclear", "unrelated"):
            with self.subTest(relationship=relationship):
                runner = Runner(relationship=relationship, judgment="neutral" if relationship == "relevant_family" else "unclear")
                self.accept(dict(DEFINITION, primary_use="Synthetic use " + relationship))
                result = self.review(runner)
                self.assertEqual(result["remaining_questions"], 0, result)
                self.review(runner)
                self.assertEqual(len(runner.calls), 1)

    def test_broad_context_does_not_become_narrow_vote(self):
        self.review(Runner(relationship="broader_context", scope="broader_context", judgment="unclear"))
        market = functional_market_context(self.memory.snapshot(), "first", as_of=NOW)
        self.assertFalse(market["market"]["sources"]["amazon"]["available"])
        panel = next(row for row in source_coverage(self.memory, concept_ids=["first"], as_of=NOW)["rows"]
                     if row["route"] == "amazon-bestsellers")
        self.assertEqual(panel["state"], "insufficient")
        self.assertEqual(panel["remaining_observations"], 0)

    def test_title_only_correction_reuses_but_changed_meaning_requires_new_review(self):
        runner = Runner(); self.review(runner)
        self.accept(dict(DEFINITION, report_title="New display title"))
        self.review(runner)
        self.assertEqual(len(runner.calls), 1)
        self.accept(dict(DEFINITION, primary_use="A meaningfully different ordinary use"))
        self.review(runner)
        self.assertEqual(len(runner.calls), 2)

    def test_concurrent_meaning_change_refuses_stale_answer_application(self):
        runner = Runner(mutate=lambda: self.accept(dict(DEFINITION, mechanism="A changed mechanism")))
        result = self.review(runner)
        self.assertTrue(any(row["state"] == "waiting" for row in result["outcomes"]), result)
        self.assertFalse(validated_scoped_evidence(self.memory.snapshot(), "first", as_of=NOW))

    def test_partial_answer_application_reuses_exact_saved_job(self):
        # The answer is applied when every record it judged has its own match row.
        from product_radar.research_scope import accept_scoped_evidence
        saves = []
        def fail_last(*args, **kwargs):
            saves.append(1)
            if len(saves) == 3:
                raise OSError("synthetic local save interruption")
            return accept_scoped_evidence(*args, **kwargs)
        runner = Runner()
        with patch("product_radar.research_scope.accept_scoped_evidence", side_effect=fail_last):
            result = self.review(runner)
        self.assertEqual(result["remaining_questions"], 1)
        final = self.review(runner)
        self.assertEqual(final["remaining_questions"], 0, final)
        self.assertEqual(len(runner.calls), 1)

    def test_global_newer_source_item_invalidates_old_scope_and_reviews_new_bytes(self):
        self.review()
        values = [dict(amazon_row(), title="Replacement unrelated row", asin="B012345670",
            link="https://www.amazon.in/dp/B012345670")]
        imported = import_capture(self.memory, source="amazon", acquisition_id="later-original", observed_at=LATER,
            mapped=map_research_rows(values, request()), capture_hash=digest(values), reference={"fixture": "synthetic"})
        latest_ref = next(ref for ref, row in self.memory.snapshot()["observations"].items()
                          if row["capture_id"] == imported["capture_id"])
        cohorts = functional_source_questions(self.memory.snapshot(), run_id="full-list", as_of=LATER)
        self.assertIn(latest_ref, {ref for row in cohorts for ref in row["unknown_ids"]})
        review_source_results(self.memory, Runner(relationship="unrelated", judgment="unclear"),
                              run_id="full-list", as_of=LATER)
        current = functional_market_context(self.memory.snapshot(), "first", as_of=LATER)
        self.assertNotIn(latest_ref, current["market"]["sources"]["amazon"]["used_ids"])

    def test_accepted_meaning_does_not_inherit_completed_legacy_market_votes(self):
        legacy_fixtures.SourceInterpretationTests.setUp(self)
        task = legacy_fixtures.SourceInterpretationTests.acquired(self, route="amazon-bestsellers")
        review_source_results(self.memory, legacy_fixtures.Runner(), run_id="full-list", as_of=NOW)
        self.assertEqual(self.memory.snapshot()["query_tasks"][task]["target_results"]["first"]["state"], "completed_relevant")
        self.refs = sorted(self.memory.snapshot()["observations"])
        self.memory.put("associations", "real-child", {"concept_id": "first", "status": "resolved",
            "semantic_id": self.memory.snapshot()["observations"][self.refs[0]]["semantic_id"]})
        self.accept()
        market = functional_market_context(self.memory.snapshot(), "first", as_of=NOW)
        self.assertFalse(market["market"]["sources"]["amazon"]["available"])
        panel = next(row for row in source_coverage(self.memory, concept_ids=["first"], as_of=NOW)["rows"]
                     if row["route"] == "amazon-bestsellers")
        self.assertEqual(panel["state"], "interpretation_pending")
        self.assertEqual(panel["remaining_observations"], 2)

    def test_programme_matching_readiness_contains_functional_cohort(self):
        from product_radar.source_interpretation import _questions
        questions = _questions(self.memory.snapshot(), run_id="full-list", as_of=NOW)
        self.assertEqual([row["kind"] for row in questions], ["functional_source_scope"])
        self.review()
        self.assertFalse(_questions(self.memory.snapshot(), run_id="full-list", as_of=NOW))

    def test_original_source_change_during_model_reply_refuses_application(self):
        def change_source():
            state = self.memory.snapshot(); old = state["observations"][self.refs[0]]
            self.memory.put("observations", self.refs[0], dict(old, facts=dict(old["facts"], rank=999)), previous=old)
        with self.assertRaisesRegex(SafetyError, "RESEARCH_CAPTURE_CONFLICT"):
            self.review(Runner(mutate=change_source))
        self.assertFalse(validated_scoped_evidence(self.memory.snapshot(), "first", as_of=NOW))

    def test_mismatched_match_capsule_does_not_certify_narrow_scope(self):
        from product_radar.research_scope import accept_scoped_evidence
        self.review(Runner(relationship="broader_context", scope="broader_context", judgment="unclear"))
        rows = validated_scoped_evidence(self.memory.snapshot(), "first", as_of=NOW)
        row = next(value for value in rows if value["method"] == FUNCTIONAL_MATCH_METHOD)
        accept_scoped_evidence(self.memory, "first", route=row["route"], scope="narrow_type",
            relationship="relevant_family", support="insufficient",
            references=[ref for ref in row["references"] if ref["table"] == "observations"],
            usable_fields=[{"reference": field["reference"], "path": field["path"]} for field in row["usable_fields"]],
            limitations=row["limitations"], reason=row["reason"], reviewer="synthetic-mismatched-adopter", as_of=NOW,
            expected_meaning_hash=row["subject"]["definition_hash"], method=FUNCTIONAL_MATCH_METHOD,
            job_id=row["job_proof"]["job_id"], job_item_id="first")
        cohorts = functional_source_questions(self.memory.snapshot(), run_id="full-list", as_of=NOW)
        self.assertEqual(sum(len(value["unknown_ids"]) for value in cohorts), 0)  # Genuine broad review remains completed.
        panel = next(value for value in source_coverage(self.memory, concept_ids=["first"], as_of=NOW)["rows"]
                     if value["route"] == "amazon-bestsellers")
        self.assertEqual(panel["state"], "insufficient")
        self.assertEqual(panel["remaining_observations"], 0)

    def test_narrow_task_and_concept_subset_does_not_bind_other_legacy_work(self):
        legacy_fixtures.SourceInterpretationTests.setUp(self)
        chosen = legacy_fixtures.SourceInterpretationTests.acquired(self, query="sink holder")
        legacy_fixtures.SourceInterpretationTests.acquired(self, ids=["second"], query="draining caddy")
        other = next(ref for ref, row in self.memory.snapshot()["query_tasks"].items()
                     if row.get("request", {}).get("query") == "draining caddy")
        before = deepcopy(self.memory.snapshot()["query_tasks"][other])
        result = review_source_results(self.memory, legacy_fixtures.Runner(), run_id="full-list", as_of=NOW,
                                      task_ids=[chosen], concept_ids=["first"])
        self.assertEqual(result["remaining_questions"], 0)
        self.assertEqual(self.memory.snapshot()["query_tasks"][other], before)
        self.assertEqual(self.memory.snapshot()["query_tasks"][chosen]["target_results"]["first"]["state"], "completed_relevant")
        for scope in ([chosen, chosen], ["not-a-task"]):
            with self.subTest(scope=scope), self.assertRaisesRegex(SafetyError, "SOURCE_REVIEW_TASK_SCOPE_INVALID"):
                review_source_results(self.memory, run_id="full-list", as_of=NOW, task_ids=scope)

    def test_functional_subset_does_not_review_other_concept_on_same_capture(self):
        state = self.memory.snapshot()
        prepare_source_tasks(self.memory, [{"route": "amazon-bestsellers", "request": state["query_tasks"][self.task_id]["request"],
            "concept_ids": ["second"]}], run_id="full-list", as_of=NOW)
        self.memory.put("associations", "second-child", {"concept_id": "second", "status": "resolved",
            "semantic_id": state["observations"][self.refs[0]]["semantic_id"]})
        accept_functional_definition(self.memory, "second", DEFINITION, basis_refs=[self.refs[0]],
            reviewer="synthetic", reviewed_at=NOW,
            expected_meaning_hash=effective_functional_definition(self.memory.snapshot(), "second")["meaning_hash"])
        result = review_source_results(self.memory, Runner(), run_id="full-list", as_of=NOW,
                                      task_ids=[self.task_id], concept_ids=["first"])
        self.assertEqual(result["remaining_questions"], 0)
        self.assertFalse(validated_scoped_evidence(self.memory.snapshot(), "second", as_of=NOW))
        self.assertTrue(functional_source_questions(self.memory.snapshot(), run_id="full-list", as_of=NOW,
                                                   task_ids=[self.task_id], concept_ids=["second"])[0]["unknown_ids"])

    def accept_second_by_association(self):
        state = self.memory.snapshot()
        self.memory.put("associations", "second-child", {"concept_id": "second", "status": "resolved",
            "semantic_id": state["observations"][self.refs[0]]["semantic_id"]})
        accept_functional_definition(self.memory, "second", DEFINITION, basis_refs=[self.refs[0]],
            reviewer="synthetic", reviewed_at=NOW,
            expected_meaning_hash=effective_functional_definition(self.memory.snapshot(), "second")["meaning_hash"])

    def scoped_reads(self, **scope):
        """Questions plus the types whose saved reviews and meaning were read."""
        import product_radar.research_scope as research_scope
        import product_radar.source_interpretation as source_interpretation
        with patch.object(research_scope, "validated_scoped_evidence",
                          wraps=research_scope.validated_scoped_evidence) as reviews, \
                patch.object(source_interpretation, "_functional_context",
                             wraps=source_interpretation._functional_context) as meanings:
            rows = functional_source_questions(self.memory.snapshot(), as_of=NOW, **scope)
        return rows, {call.args[1] for call in reviews.call_args_list}, {call.args[1] for call in meanings.call_args_list}

    def test_run_scope_does_not_read_types_without_a_task_in_that_run(self):
        # 15 Oct live run: every per-scope match pass re-validated all ~30
        # accepted types six times (about 4 minutes) to find 1-5 types' cohorts.
        self.accept_second_by_association()
        expected = functional_source_questions(self.memory.snapshot(), run_id="full-list", as_of=NOW,
                                               concept_ids=["first"])
        rows, reviewed, meanings = self.scoped_reads(run_id="full-list")
        self.assertEqual(rows, expected)
        self.assertEqual({row["concept_id"] for row in rows}, {"first"})
        self.assertEqual((reviewed, meanings), ({"first"}, {"first"}))
        rows, reviewed, _ = self.scoped_reads(task_ids=[self.task_id])
        self.assertEqual(rows, expected)
        self.assertEqual(reviewed, {"first"})
        # Without a run or task scope, the saved association still nominates it.
        rows, reviewed, _ = self.scoped_reads()
        self.assertEqual({row["concept_id"] for row in rows}, {"first", "second"})
        self.assertEqual(reviewed, {"first", "second"})

    def test_type_with_only_uncaptured_tasks_has_no_cohort_and_no_saved_review_read(self):
        self.accept_second_by_association()
        prepare_source_tasks(self.memory, [{"route": "amazon-search", "request": {"query": "draining caddy", "country": "IN"},
            "concept_ids": ["second"]}], run_id="full-list", as_of=NOW)
        rows, reviewed, meanings = self.scoped_reads(run_id="full-list")
        self.assertEqual({row["concept_id"] for row in rows}, {"first"})
        self.assertIn("second", meanings)  # Targeted: its meaning is still checked.
        self.assertEqual(reviewed, {"first"})

