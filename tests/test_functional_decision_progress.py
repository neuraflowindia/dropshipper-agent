"""Post-commit decision activity with real temporary writers; no live calls."""

from contextlib import redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import test_catalogue_scope_review as catalogue_fixtures
import test_functional_alias_review as alias_review_fixtures
import test_functional_aliases as alias_fixtures
import test_functional_definition_review as definition_fixtures
import test_functional_example_review as example_fixtures
import test_functional_source_interpretation as source_fixtures
import test_research_scope as scope_fixtures
import test_source_interpretation as legacy_source_fixtures
from product_radar import operations, programme
from product_radar.functional_types import (current_example_memberships,
    effective_functional_definition, prepare_example_memberships)
from product_radar.models import digest
from product_radar.operations import (operation_run, operation_writer,
    read_operation_status, record_event, record_progress, record_saved_decision)
from product_radar.market_validation import functional_market_context
from product_radar.research_questions import prepare_research_questions
from product_radar.research_records import CATALOGUE_SCOPE_KIND, SCOPED_EVIDENCE_KIND
from product_radar.research_ai import current_attempt
from product_radar.source_interpretation import (FUNCTIONAL_MATCH_METHOD,
    FUNCTIONAL_SUPPORT_METHOD, review_source_results)
from product_radar.storage import load_json
from product_radar.work_turns import bind_work_turn, close_work_turn, open_work_turn


KINDS = (
    "functional-definition-v1",
    "functional-definition-review-v1",
    "functional-example-review-v1",
    CATALOGUE_SCOPE_KIND,
    "functional-alias-review-v1",
    "functional-alias-v1",
)


class FunctionalDecisionProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def fixture(self, case):
        value = case()
        value.setUp()
        self.addCleanup(value.doCleanups)
        return value

    def rows(self, recorder):
        path = recorder.folder / (recorder.run_id + ".jsonl")
        return [json.loads(line) for line in path.read_text().splitlines()]

    def saved(self, recorder):
        return [row for row in self.rows(recorder)
                if row["event"] == "research_decision_saved"]

    def kind_records(self, memory, kind):
        return {rid: row for rid, row in memory.snapshot()["source_checks"].items()
                if row.get("kind") == kind}

    def save_case(self, kind):
        """Only fixture setup runs before the operation recorder is installed."""
        if kind in {"functional-definition-v1", "functional-definition-review-v1"}:
            fixture = self.fixture(definition_fixtures.FunctionalDefinitionReviewTests)
            runner = definition_fixtures.Runner(
                decision="unclear" if kind.endswith("review-v1") else "prepared")
            run = lambda: fixture.run_review(runner, [fixture.cids[0]])
        elif kind == "functional-example-review-v1":
            fixture = self.fixture(example_fixtures.FunctionalExampleReviewTests)
            runner = example_fixtures.Runner()
            run = lambda: fixture.review(runner)
        elif kind == CATALOGUE_SCOPE_KIND:
            fixture = self.fixture(scope_fixtures.ScopeTests)
            run = fixture.catalogue
        elif kind == "functional-alias-review-v1":
            fixture = self.fixture(alias_review_fixtures.FunctionalAliasReviewTests)
            fixture.different_wording()
            runner = alias_review_fixtures.Runner()
            run = lambda: fixture.run_review(runner)
        else:
            self.assertEqual(kind, "functional-alias-v1")
            fixture = self.fixture(alias_fixtures.FunctionalAliasTests)
            run = fixture.accept
        return fixture.memory, run

    def test_all_six_real_writers_credit_new_records_once_and_not_unchanged_reuse(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                memory, save = self.save_case(kind)
                original = self.kind_records(memory, kind)
                with operation_run(self.root) as recorder, operation_writer():
                    record_event("stage_started", stage="match")
                    result = save()
                    new = self.kind_records(memory, kind)
                    added = set(new) - set(original)
                    events = self.saved(recorder)
                    self.assertEqual(len(added), 1)
                    self.assertEqual({row["record_id"] for row in events}, added)
                    self.assertEqual({row["record_kind"] for row in events}, {kind})
                    self.assertTrue(all(row["state"] == "saved"
                                        and row["useful_result"] is True for row in events))
                    first = read_operation_status(self.root)
                    self.assertEqual(first["counts"]["completed_results"], 1)
                    self.assertIsNotNone(first["last_useful_result_at"])
                    before_reuse = memory.snapshot_with_provenance()
                    save()
                    self.assertEqual(memory.snapshot_with_provenance(), before_reuse)
                    self.assertEqual(len(self.saved(recorder)), 1)
                    self.assertEqual(read_operation_status(self.root)["last_useful_result_at"],
                                     first["last_useful_result_at"])
                    # Existing nested helper shapes may contain old accepted
                    # decisions. The stage summary must not credit them again.
                    record_progress({"stage": "match", "phase_state": "finished", "result": {
                        "outcomes": [], "reused": [], "functional_definitions": result,
                        "functional_examples": result, "catalogue_scopes": result,
                        "functional_aliases": result}})
                    self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 1)

    def test_all_six_writers_do_not_credit_false_commit_reply(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                memory, save = self.save_case(kind)
                original = memory.commit
                before = self.kind_records(memory, kind)
                attempted = []
                def refuse(eid, changes, *, validate=None):
                    if any(row.get("table") == "source_checks"
                           and row.get("value", {}).get("kind") == kind for row in changes):
                        attempted.append(eid)
                        return False
                    return original(eid, changes, validate=validate)
                with operation_run(self.root) as recorder, operation_writer(), \
                        patch.object(memory, "commit", side_effect=refuse):
                    save()
                    self.assertEqual(len(attempted), 1)
                    self.assertEqual(self.kind_records(memory, kind), before)
                    self.assertEqual(self.saved(recorder), [])
                    self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 0)
                    self.assertIsNone(read_operation_status(self.root)["last_useful_result_at"])

    def test_all_six_writers_do_not_credit_a_failed_canonical_save(self):
        for kind in KINDS:
            with self.subTest(kind=kind):
                memory, save = self.save_case(kind)
                original = memory.commit
                before = self.kind_records(memory, kind)
                attempted = []
                def fail(eid, changes, *, validate=None):
                    if any(row.get("table") == "source_checks"
                           and row.get("value", {}).get("kind") == kind for row in changes):
                        attempted.append(eid)
                        raise OSError("Fictional local decision-save failure")
                    return original(eid, changes, validate=validate)
                with operation_run(self.root) as recorder, operation_writer(), \
                        patch.object(memory, "commit", side_effect=fail):
                    if kind in {CATALOGUE_SCOPE_KIND, "functional-alias-v1"}:
                        with self.assertRaises(OSError):
                            save()
                    else:
                        save()
                    self.assertEqual(len(attempted), 1)
                    self.assertEqual(self.kind_records(memory, kind), before)
                    self.assertEqual(self.saved(recorder), [])
                    self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 0)
                    self.assertIsNone(read_operation_status(self.root)["last_useful_result_at"])

    def test_definition_model_reply_has_no_credit_before_its_actual_save(self):
        fixture = self.fixture(definition_fixtures.FunctionalDefinitionReviewTests)
        runner = definition_fixtures.Runner()
        original = fixture.memory.commit
        observed = []
        def inspect(eid, changes, *, validate=None):
            definitions = [row["value"] for row in changes
                           if row.get("value", {}).get("kind") == "functional-definition-v1"]
            if definitions:
                observed.append(read_operation_status(self.root))
                self.assertEqual(len(runner.calls), 1)
                jid = definitions[0]["job_id"]
                job = fixture.memory.snapshot()["jobs"][jid]
                receipt = load_json(current_attempt(fixture.memory.root / "ai_jobs" / jid) / "result.json")
                self.assertEqual(job["request"]["prompt_version"], definition_fixtures.PROMPT_VERSION)
                self.assertEqual(receipt["request_hash"], digest(job["request"]))
                self.assertEqual(len(receipt["response"]["results"]), 1)
            return original(eid, changes, validate=validate)
        with operation_run(self.root), operation_writer(), \
                patch.object(fixture.memory, "commit", side_effect=inspect):
            fixture.run_review(runner, [fixture.cids[0]])
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0]["counts"]["completed_results"], 0)
            self.assertIsNone(observed[0]["last_useful_result_at"])
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 1)

    def test_examples_different_and_unclear_are_completed_judgments_not_membership(self):
        for decision in ("different", "unclear"):
            with self.subTest(decision=decision):
                fixture = self.fixture(example_fixtures.FunctionalExampleReviewTests)
                runner = example_fixtures.Runner(decision=decision)
                with operation_run(self.root) as recorder, operation_writer():
                    result = fixture.review(runner)
                    self.assertFalse(result["failures"])
                    saved = self.saved(recorder)
                    self.assertEqual(len(saved), 1)
                    record = fixture.memory.snapshot()["source_checks"][saved[0]["record_id"]]
                    self.assertEqual({row["decision"] for row in record["result"]["decisions"]}, {decision})
                    prepare_example_memberships(fixture.memory, fixture.cid, as_of=example_fixtures.NOW)
                    self.assertEqual(current_example_memberships(fixture.memory.snapshot(), fixture.cid), [])
                    self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 1)
                    fixture.review(runner)
                    self.assertEqual(len(self.saved(recorder)), 1)
                    self.assertEqual(len(runner.calls), 1)

    def test_alias_different_and_unclear_are_saved_without_applying_a_relationship(self):
        for decision in ("different", "unclear"):
            with self.subTest(decision=decision):
                fixture = self.fixture(alias_review_fixtures.FunctionalAliasReviewTests)
                fixture.different_wording()
                runner = alias_review_fixtures.Runner(decision=decision)
                with operation_run(self.root) as recorder, operation_writer():
                    result = fixture.run_review(runner)
                    self.assertFalse(result["failures"])
                    self.assertEqual(len(self.saved(recorder)), 1)
                    event = self.saved(recorder)[0]
                    record = fixture.memory.snapshot()["source_checks"][event["record_id"]]
                    self.assertEqual(record["result"]["decision"], decision)
                    self.assertFalse(self.kind_records(fixture.memory, "functional-alias-v1"))
                    fixture.run_review(runner)
                    self.assertEqual(len(self.saved(recorder)), 1)
                    self.assertEqual(len(runner.calls), 1)

    def test_real_catalogue_review_unclear_is_saved_once_without_a_positive_claim(self):
        fixture = self.fixture(catalogue_fixtures.CatalogueScopeTests)
        runner = catalogue_fixtures.ScopeRunner("unclear")
        with operation_run(self.root) as recorder, operation_writer():
            result = fixture.review(runner)
            self.assertEqual(result["outcomes"][0]["relationship"], "unclear")
            self.assertEqual(len(self.saved(recorder)), 1)
            self.assertEqual(self.saved(recorder)[0]["record_kind"], CATALOGUE_SCOPE_KIND)
            fixture.review(runner)
            self.assertEqual(len(self.saved(recorder)), 1)
            self.assertEqual(len(runner.calls), 1)

    def test_invalid_model_answer_does_not_create_a_saved_decision_or_useful_credit(self):
        fixture = self.fixture(definition_fixtures.FunctionalDefinitionReviewTests)
        def invalidate(rows):
            rows[0]["references"] = ["invented-child"]
        runner = definition_fixtures.Runner(mutate=invalidate)
        before = fixture.memory.snapshot()["source_checks"]
        with operation_run(self.root) as recorder, operation_writer():
            result = fixture.run_review(runner, [fixture.cids[0]])
            self.assertFalse(result["accepted"])
            self.assertTrue(result["failures"])
            self.assertEqual(len(runner.calls), 1)
            self.assertEqual(fixture.memory.snapshot()["source_checks"], before)
            self.assertEqual(self.saved(recorder), [])
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 0)
            self.assertIsNone(read_operation_status(self.root)["last_useful_result_at"])

    def test_alias_wait_for_existing_reports_is_not_a_saved_relationship(self):
        fixture = self.fixture(alias_fixtures.FunctionalAliasTests)
        fixture.memory.put("issue_reservations", "held-report", {"issue_id": "held-report",
            "client_id": "default", "state": "uploaded", "products": [
                {"concept_id": "bear", "status": "New"}, {"concept_id": "bird", "status": "New"}]})
        before = fixture.memory.snapshot_with_provenance()
        with operation_run(self.root) as recorder, operation_writer():
            result = fixture.accept()
            self.assertIs(result["applied"], False)
            self.assertEqual(result["decision"], "release_review_required")
            self.assertEqual(result["affected_issue_ids"], ["held-report"])
            self.assertEqual(fixture.memory.snapshot_with_provenance(), before)
            self.assertEqual(self.saved(recorder), [])
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 0)

    def test_shared_scope_writer_credits_new_source_scope_but_not_its_reuse(self):
        fixture = self.fixture(scope_fixtures.ScopeTests)
        before = fixture.memory.snapshot_with_provenance()
        with operation_run(self.root) as recorder, operation_writer():
            saved = fixture.evidence()
            self.assertIs(saved["reused"], False)
            self.assertNotEqual(fixture.memory.snapshot_with_provenance(), before)
            self.assertEqual(len(self.saved(recorder)), 1)
            self.assertEqual(self.saved(recorder)[0]["record_kind"], SCOPED_EVIDENCE_KIND)
            self.assertEqual(self.saved(recorder)[0]["record_id"], saved["id"])
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 1)
            before_reuse = fixture.memory.snapshot_with_provenance()
            self.assertIs(fixture.evidence()["reused"], True)
            self.assertEqual(fixture.memory.snapshot_with_provenance(), before_reuse)
            self.assertEqual(len(self.saved(recorder)), 1)

    def test_research_question_preparation_is_not_a_completed_saved_judgment(self):
        fixture = self.fixture(scope_fixtures.ScopeTests)
        state = fixture.memory.snapshot()
        with operation_run(self.root) as recorder, operation_writer():
            result = prepare_research_questions(fixture.memory, [{
                "subject": {"kind": "concept", "concept_id": fixture.cid,
                            "definition_hash": effective_functional_definition(state, fixture.cid)["meaning_hash"]},
                "route": "meta-ads", "purpose": "validation",
                "request": {"query": "draining sink caddy", "country": "IN"}}], as_of=scope_fixtures.NOW)
            self.assertEqual(result["created"], 1)
            self.assertFalse(result["collector_dispatched"])
            self.assertEqual(self.saved(recorder), [])
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 0)

    def test_functional_source_partial_saves_are_immediate_and_recovery_counts_only_new_records(self):
        fixture = self.fixture(source_fixtures.FunctionalSourceInterpretationTests)
        runner = source_fixtures.Runner()
        original = fixture.memory.commit
        before_saves = []
        parent_fields = ("step", "phase", "phase_started_at", "phase_deadline_seconds", "next_action")
        def fail_support(eid, changes, *, validate=None):
            for change in changes:
                record = change.get("value", {})
                if record.get("kind") == SCOPED_EVIDENCE_KIND:
                    before_saves.append(read_operation_status(self.root))
                    if record["method"] == FUNCTIONAL_SUPPORT_METHOD:
                        raise OSError("Fictional interruption before the source-support save")
            return original(eid, changes, validate=validate)
        before = fixture.memory.snapshot()
        with operation_run(self.root) as recorder, operation_writer():
            with patch.object(operations, "utc_now", return_value="2026-10-12T10:00:00+00:00"):
                record_event("stage_started", stage="match")
                record_event("phase_started", phase="model_call", deadline_seconds=180,
                             next_action="preserve_source_review_next_action")
            parent = read_operation_status(self.root)
            with patch.object(operations, "utc_now", return_value="2026-10-12T10:00:30+00:00"), \
                    patch.object(fixture.memory, "commit", side_effect=fail_support):
                partial = fixture.review(runner)
            self.assertEqual([row["counts"]["completed_results"] for row in before_saves], [0, 1, 2, 3])
            self.assertEqual(partial["remaining_questions"], 1)
            self.assertTrue(any(row["state"] == "waiting" for row in partial["outcomes"]))
            first = read_operation_status(self.root)
            self.assertEqual(first["counts"]["completed_results"], 3)
            self.assertEqual(first["last_useful_result_at"], "2026-10-12T10:00:30+00:00")
            for field in parent_fields:
                self.assertEqual(first[field], parent[field], field)
            first_records = self.kind_records(fixture.memory, SCOPED_EVIDENCE_KIND)
            self.assertEqual(len(first_records), 3)
            self.assertEqual({row["method"] for row in first_records.values()}, {FUNCTIONAL_MATCH_METHOD})
            with patch.object(operations, "utc_now", return_value="2026-10-12T10:01:00+00:00"):
                result = fixture.review(runner)
            self.assertEqual(result["remaining_questions"], 0)
            self.assertEqual(len(runner.calls), 1)
            records = self.kind_records(fixture.memory, SCOPED_EVIDENCE_KIND)
            self.assertEqual(len(records), 4)
            self.assertEqual(len(set(records) - set(first_records)), 1)
            self.assertEqual({row["record_id"] for row in self.saved(recorder)}, set(records))
            current = read_operation_status(self.root)
            self.assertEqual(current["counts"]["completed_results"], 4)
            self.assertEqual(current["last_useful_result_at"], "2026-10-12T10:01:00+00:00")
            for field in parent_fields:
                self.assertEqual(current[field], parent[field], field)
            for table in ("query_tasks", "captures", "observations", "assessments", "market_reviews"):
                self.assertEqual(fixture.memory.snapshot()[table], before[table], table)
            # Four saved decisions describe one source/product review, not
            # four products. The returned aggregate adds no fifth credit.
            self.assertTrue(all(isinstance(row["scoped_evidence_refs"], list)
                                for row in result["outcomes"] if row["state"] == "reviewed"))
            record_progress({"stage": "match", "phase_state": "finished", "result": result})
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 4)
            before_reuse = fixture.memory.snapshot_with_provenance()
            repeated = fixture.review(runner)
            self.assertEqual(fixture.memory.snapshot_with_provenance(), before_reuse)
            record_progress({"stage": "match", "phase_state": "finished", "result": repeated})
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 4)
            self.assertEqual(len(self.saved(recorder)), 4)
            self.assertEqual(len(runner.calls), 1)

    def test_functional_source_false_and_failed_scope_commits_have_no_result_or_summary_credit(self):
        for failure in ("false", "raises"):
            with self.subTest(failure=failure):
                fixture = self.fixture(source_fixtures.FunctionalSourceInterpretationTests)
                runner = source_fixtures.Runner()
                original = fixture.memory.commit
                visits = []
                def refuse(eid, changes, *, validate=None):
                    if any(change.get("value", {}).get("kind") == SCOPED_EVIDENCE_KIND
                           for change in changes):
                        visits.append(eid)
                        if failure == "raises":
                            raise OSError("Fictional source-scope save failure")
                        return False
                    return original(eid, changes, validate=validate)
                with operation_run(self.root) as recorder, operation_writer():
                    with patch.object(fixture.memory, "commit", side_effect=refuse):
                        result = fixture.review(runner)
                    self.assertEqual(len(visits), 4 if failure == "false" else 1)
                    self.assertEqual(self.kind_records(fixture.memory, SCOPED_EVIDENCE_KIND), {})
                    self.assertEqual(self.saved(recorder), [])
                    record_progress({"stage": "match", "phase_state": "finished", "result": result})
                    self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 0)
                    self.assertIsNone(read_operation_status(self.root)["last_useful_result_at"])
                    recovered = fixture.review(runner)
                    self.assertEqual(recovered["remaining_questions"], 0)
                    self.assertEqual(len(runner.calls), 1)
                    self.assertEqual(len(self.saved(recorder)), 4)
                    record_progress({"stage": "match", "phase_state": "finished", "result": recovered})
                    self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 4)

    def test_negative_and_unclear_functional_source_judgments_count_without_changing_their_opinion(self):
        for relationship, judgment, support in (
            ("relevant_family", "weak", "not_positive"),
            ("unclear", "unclear", "insufficient"),
            ("unrelated", "unclear", "insufficient"),
        ):
            with self.subTest(relationship=relationship, judgment=judgment):
                fixture = self.fixture(source_fixtures.FunctionalSourceInterpretationTests)
                runner = source_fixtures.Runner(relationship=relationship, judgment=judgment)
                with operation_run(self.root) as recorder, operation_writer():
                    result = fixture.review(runner)
                    self.assertEqual(result["remaining_questions"], 0)
                    self.assertEqual(len(self.saved(recorder)), 4)
                    records = self.kind_records(fixture.memory, SCOPED_EVIDENCE_KIND)
                    opinions = [row for row in records.values() if row["method"] == FUNCTIONAL_SUPPORT_METHOD]
                    self.assertEqual(len(opinions), 1)
                    self.assertEqual(opinions[0]["support"], support)
                    market = functional_market_context(fixture.memory.snapshot(), "first", as_of=source_fixtures.NOW)
                    signal = market["market"]["sources"]["amazon"]
                    self.assertNotEqual(signal.get("evidence_support"), "reasonably_positive")
                    if judgment == "weak":
                        # Observed bestseller placement stays observed; the
                        # activity update must not turn E's weak opinion positive.
                        self.assertEqual(signal.get("support_judgment"), "weak")
                    else:
                        self.assertIs(signal["available"], False)
                        self.assertIsNot(signal["positive"], True)
                    before_reuse = fixture.memory.snapshot_with_provenance()
                    repeated = fixture.review(runner)
                    self.assertEqual(fixture.memory.snapshot_with_provenance(), before_reuse)
                    record_progress({"stage": "match", "phase_state": "finished", "result": result})
                    record_progress({"stage": "match", "phase_state": "finished", "result": repeated})
                    self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 4)
                    self.assertEqual(len(runner.calls), 1)

    def test_plain_legacy_source_review_keeps_its_original_summary_credit(self):
        fixture = self.fixture(legacy_source_fixtures.SourceInterpretationTests)
        fixture.acquired(route="amazon-bestsellers")
        runner = legacy_source_fixtures.Runner()
        with operation_run(self.root) as recorder, operation_writer():
            result = review_source_results(fixture.memory, runner, run_id="full-list", as_of=legacy_source_fixtures.NOW)
            reviewed = [row for row in result["outcomes"] if row["state"] == "reviewed"]
            self.assertEqual(len(reviewed), 1)
            self.assertNotIn("scoped_evidence_refs", reviewed[0])
            self.assertEqual(self.saved(recorder), [])
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 0)
            record_progress({"stage": "match", "phase_state": "finished", "result": result})
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 1)
            repeated = review_source_results(fixture.memory, runner, run_id="full-list", as_of=legacy_source_fixtures.NOW)
            record_progress({"stage": "match", "phase_state": "finished", "result": repeated})
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 1)
            self.assertEqual(len(runner.calls), 1)

    def test_functional_reference_list_alone_never_counts_a_completed_save(self):
        for refs in ([], ["previously-saved-scope"]):
            with self.subTest(refs=refs), operation_run(self.root), operation_writer():
                record_progress({"stage": "match", "phase_state": "finished", "result": {
                    "outcomes": [{"state": "reviewed", "scoped_evidence_refs": refs}]}})
                self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 0)
                self.assertIsNone(read_operation_status(self.root)["last_useful_result_at"])

    def test_mixed_actual_legacy_and_functional_outcomes_credit_only_unsignalled_legacy_summary(self):
        functional = self.fixture(source_fixtures.FunctionalSourceInterpretationTests)
        legacy = self.fixture(legacy_source_fixtures.SourceInterpretationTests)
        legacy.acquired(route="amazon-bestsellers")
        with operation_run(self.root) as recorder, operation_writer():
            current_result = functional.review(source_fixtures.Runner())
            older_result = review_source_results(legacy.memory, legacy_source_fixtures.Runner(),
                run_id="full-list", as_of=legacy_source_fixtures.NOW)
            self.assertEqual(len(self.saved(recorder)), 4)
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 4)
            mixed = {"outcomes": current_result["outcomes"] + older_result["outcomes"]}
            self.assertEqual(sum(row["state"] == "reviewed" for row in mixed["outcomes"]), 2)
            record_progress({"stage": "match", "phase_state": "finished", "result": mixed})
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 5)
            self.assertEqual(len(self.saved(recorder)), 4)

    def test_real_match_join_has_immediate_credit_and_no_stage_finish_or_replay_double_count(self):
        fixture = self.fixture(alias_review_fixtures.FunctionalAliasReviewTests)
        record = {"programme_id": "synthetic-decision-progress", "concept_ids": ["bird"],
                  "source_run_id": "synthetic-sources", "deferrals": {}, "cycles": 0}
        context = {"runner": None, "at": alias_review_fixtures.REVIEWED, "match_limit": 5,
                   "retry_seconds": 300, "stage": "match"}
        turn = open_work_turn(fixture.memory, record["programme_id"], at=context["at"])
        self.addCleanup(close_work_turn, fixture.memory, turn["id"], at=context["at"],
                        reason="Synthetic saved decision progress complete")
        with operation_run(self.root) as recorder, operation_writer(), \
                bind_work_turn(fixture.memory, turn["id"]), \
                patch("product_radar.source_interpretation._questions", return_value=[]), \
                patch("product_radar.source_interpretation.review_source_results",
                      return_value={"outcomes": [], "reused": []}):
            programme._observe(record, context, phase_state="started")
            result = programme._match(fixture.memory, record, context)
            self.assertTrue(result["functional_aliases"]["outcomes"][0]["applied"])
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 1)
            programme._observe(record, context, phase_state="finished", result=deepcopy(result))
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 1)
            programme._match(fixture.memory, record, context)
            self.assertEqual(len(self.saved(recorder)), 1)
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 1)

    def test_parent_phase_deadline_step_and_next_action_are_not_reset(self):
        memory, save = self.save_case("functional-definition-v1")
        with operation_run(self.root) as recorder, operation_writer():
            with patch.object(operations, "utc_now", return_value="2026-10-12T10:00:00+00:00"):
                record_event("stage_started", stage="match")
                record_event("phase_started", phase="model_call", deadline_seconds=180,
                             next_action="preserve_existing_next_action")
            before = read_operation_status(self.root)
            with patch.object(operations, "utc_now", return_value="2026-10-12T10:00:30+00:00"):
                save()
            current = read_operation_status(self.root)
            for field in ("step", "phase", "phase_started_at", "phase_deadline_seconds", "next_action"):
                self.assertEqual(current[field], before[field], field)
            self.assertEqual(current["last_useful_result_at"], "2026-10-12T10:00:30+00:00")
            self.assertEqual(current["counts"]["completed_results"], 1)
            event = self.saved(recorder)[0]
            self.assertFalse({"stage", "phase", "next_phase", "deadline_seconds", "next_action"} & set(event))

    def test_helper_requires_actual_boolean_true_and_does_not_change_research_authority(self):
        fixture = self.fixture(scope_fixtures.ScopeTests)
        before = fixture.memory.snapshot_with_provenance()
        with operation_run(self.root) as recorder, operation_writer():
            for committed in (False, None, 0, 1, "true", [], {}, object()):
                record_saved_decision(committed, record_kind=CATALOGUE_SCOPE_KIND,
                                      record_id="fixture-decision", concept_id=fixture.cid)
            self.assertEqual(self.saved(recorder), [])
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 0)
            record_saved_decision(True, record_kind=CATALOGUE_SCOPE_KIND,
                                  record_id="fixture-decision", concept_id=fixture.cid)
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 1)
        self.assertEqual(fixture.memory.snapshot_with_provenance(), before)

    def test_helper_without_active_recorder_is_an_inert_no_op(self):
        with patch.object(operations.OperationRun, "emit", side_effect=AssertionError("No recorder exists")):
            record_saved_decision(True, record_kind="functional-definition-v1", record_id="fixture-decision")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_private_or_invalid_metadata_is_not_written(self):
        with operation_run(self.root) as recorder, operation_writer():
            for values in (
                {"record_kind": "https://private.example/secret-kind", "record_id": "private record text",
                 "concept_id": "password=private-secret"},
                {"record_kind": ["private-list"], "record_id": {"private": "object"}, "concept_id": 1},
                {"record_kind": "private-overlong-" + "x" * 201, "record_id": "y" * 201, "concept_id": None},
            ):
                record_saved_decision(True, **values)
            events = self.saved(recorder)
            self.assertEqual(len(events), 3)
            self.assertTrue(all(not ({"record_kind", "record_id", "concept_id"} & set(row)) for row in events))
        raw = json.dumps(self.rows(recorder))
        for private in ("private.example", "private record", "private-secret", "private-list", "private-overlong"):
            self.assertNotIn(private, raw)

    def test_diagnostic_emit_failure_cannot_undo_a_real_saved_definition(self):
        memory, save = self.save_case("functional-definition-v1")
        notice = io.StringIO()
        with redirect_stderr(notice), operation_run(self.root) as recorder, operation_writer():
            original = recorder.emit
            def fail(event, **values):
                if event == "research_decision_saved":
                    raise OSError("Fictional diagnostic-write failure")
                return original(event, **values)
            with patch.object(recorder, "emit", side_effect=fail):
                result = save()
            self.assertEqual(len(result["accepted"]), 1)
            self.assertEqual(len(self.kind_records(memory, "functional-definition-v1")), 1)
            self.assertTrue(recorder.recording_failed)
            self.assertEqual(self.saved(recorder), [])
            self.assertNotIn("Fictional diagnostic", notice.getvalue())
            before = memory.snapshot_with_provenance()
            save()
            self.assertEqual(memory.snapshot_with_provenance(), before)
            self.assertEqual(self.saved(recorder), [])

    def test_owner_interrupts_propagate_after_save_without_rolling_back_the_decision(self):
        for error in (KeyboardInterrupt("Fictional owner stop"), SystemExit(130)):
            with self.subTest(error=type(error).__name__):
                memory, save = self.save_case("functional-definition-v1")
                with operation_run(self.root) as recorder, operation_writer():
                    original = recorder.emit
                    def interrupt(event, **values):
                        if event == "research_decision_saved":
                            raise error
                        return original(event, **values)
                    with patch.object(recorder, "emit", side_effect=interrupt), \
                            self.assertRaises(type(error)) as caught:
                        save()
                    self.assertIs(caught.exception, error)
                    self.assertEqual(len(self.kind_records(memory, "functional-definition-v1")), 1)
                    self.assertEqual(self.saved(recorder), [])

    def test_saved_sibling_is_visible_before_a_later_sibling_save_fails(self):
        fixture = self.fixture(definition_fixtures.FunctionalDefinitionReviewTests)
        self.assertGreaterEqual(len(fixture.cids), 2)
        ids = fixture.cids[:2]
        original = fixture.memory.commit
        visited = []
        statuses = []
        def fail_second(eid, changes, *, validate=None):
            for row in changes:
                if row.get("value", {}).get("kind") == "functional-definition-v1":
                    visited.append(row["value"]["concept_id"])
                    if len(visited) == 2:
                        statuses.append(read_operation_status(self.root))
                        raise OSError("Fictional second sibling save failure")
            return original(eid, changes, validate=validate)
        runner = definition_fixtures.Runner()
        with operation_run(self.root) as recorder, operation_writer(), \
                patch.object(fixture.memory, "commit", side_effect=fail_second):
            result = fixture.run_review(runner, ids)
            self.assertEqual(visited, ids)
            self.assertEqual(set(result["accepted"]), {ids[0]})
            self.assertIn(ids[1], result["failures"])
            self.assertEqual(len(statuses), 1)
            self.assertEqual(statuses[0]["counts"]["completed_results"], 1)
            self.assertIsNotNone(statuses[0]["last_useful_result_at"])
            self.assertEqual(len(self.saved(recorder)), 1)
            self.assertEqual(self.saved(recorder)[0]["concept_id"], ids[0])
            state = fixture.memory.snapshot()
            self.assertTrue(effective_functional_definition(state, ids[0])["accepted"])
            self.assertFalse(effective_functional_definition(state, ids[1])["accepted"])
            self.assertEqual(len(runner.calls), 1)


if __name__ == "__main__":
    unittest.main()
