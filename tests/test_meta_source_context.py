"""Original Meta query/filter context; all browser/model replies are synthetic."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from product_radar.functional_types import accept_functional_definition, effective_functional_definition
from product_radar.market_validation import functional_market_context
from product_radar.meta_browser import meta_direct_source_handlers, prepare_meta_query
from product_radar.models import SafetyError, canonical, digest
from product_radar.research_ai import e_contract
from product_radar.research_scope import meta_capture_context, validated_scoped_evidence
from product_radar.source_coverage import execute_source_run, prepare_source_tasks
from product_radar.source_interpretation import _item, functional_source_questions, review_source_results
from test_functional_queries import DEFINITION
from test_functional_source_interpretation import Runner
import test_source_interpretation as legacy_fixtures
from test_source_interpretation import NOW


class MetaSourceContextTests(unittest.TestCase):
    def setUp(self):
        legacy_fixtures.SourceInterpretationTests.setUp(self)
        self.calls = []
        self.request = {"query": "draining sink caddy", "country": "IN"}
        native_request = prepare_meta_query(self.request["query"])
        self.snapshot = {"url": native_request["url"], "title": "Meta Ad Library", "text": "Ad Library",
            "cards": [{"ad_id": "123456", "text": "Active\nLibrary ID: 123456\nStarted running on Oct 1, 2026\nDraining sink caddy",
                "links": [{"url": "https://www.facebook.com/profile.php?id=998877", "text": "Supplier"}],
                "images": []}]}
        def browser(url, expression, folder, **kwargs):
            self.calls.append(url)
            return {"snapshot": self.snapshot, "navigation_status": 200, "navigation_started": True,
                "owned_process_exited": True, "completed_at": NOW}
        self.handlers = {"meta-ads": meta_direct_source_handlers(self.memory, browser=browser)}
        prepared = prepare_source_tasks(self.memory, [{"route": "meta-ads", "request": self.request,
            "concept_ids": ["first"]}], run_id="original-meta", as_of=NOW)
        self.task_id = prepared["task_ids"][0]
        execute_source_run(self.memory, "original-meta", handlers=self.handlers, as_of=NOW,
            bounded_call=lambda callback, context, timeout: callback(context))
        state = self.memory.snapshot()
        self.observation_id = next(iter(state["observations"]))
        self.capture_id = state["observations"][self.observation_id]["capture_id"]
        self.receipt_id = state["query_tasks"][self.task_id]["receipt_refs"][0]
        self.memory.put("associations", "actual-meta-child", {"concept_id": "first", "status": "resolved",
            "semantic_id": state["observations"][self.observation_id]["semantic_id"]})
        accept_functional_definition(self.memory, "first", DEFINITION, basis_refs=[self.observation_id],
            reviewer="synthetic", reviewed_at=NOW,
            expected_meaning_hash=effective_functional_definition(self.memory.snapshot(), "first")["meaning_hash"])

    def payload(self):
        state = self.memory.snapshot()
        question = functional_source_questions(state, run_id="original-meta", as_of=NOW)[0]
        return {"mode": "marketplace_relevance_only", "relevance_style": "functional-source-scope-v4",
            "criteria": [], "items": [_item(state, question, 20)]}

    def review(self, runner=None):
        return review_source_results(self.memory, runner or Runner(), run_id="original-meta", as_of=NOW)

    def test_installed_original_receipt_supplies_actual_task_request_without_paths_or_new_call(self):
        state = self.memory.snapshot(); runner = Runner()
        self.assertEqual(self.review(runner)["remaining_questions"], 0)
        context = runner.calls[0]["items"][0]["observations"][0]["capture_context"]
        self.assertEqual(context["request"], self.request)
        self.assertEqual(context["request_basis"], "receipt_bound_task_request")
        self.assertEqual(context["observed_at"], NOW)
        self.assertEqual(context["capture"], {"table": "captures", "id": self.capture_id,
            "hash": digest(state["captures"][self.capture_id])})
        self.assertEqual(context["original_task"]["hash"], digest(state["query_tasks"][self.task_id]))
        self.assertEqual(context["original_receipt"]["hash"], digest(state["source_checks"][self.receipt_id]))
        self.assertNotIn(str(self.memory.root).encode(), canonical(context))
        self.assertEqual(len(self.calls), 1)
        self.review(runner)
        self.assertEqual((len(self.calls), len(runner.calls)), (1, 1))
        self.assertTrue(functional_market_context(self.memory.snapshot(), "first", as_of=NOW)["market"]["sources"]["meta"]["available"])
        for table in ("query_tasks", "observations", "captures"):
            self.assertEqual(self.memory.snapshot()[table], state[table])

    def test_orphan_import_has_explicit_missing_context_not_title_inference(self):
        state = self.memory.snapshot()
        orphan = deepcopy(state)
        orphan["query_tasks"] = {}; orphan["source_checks"] = {}
        context = meta_capture_context(orphan, self.observation_id)
        self.assertEqual(context["state"], "not_saved")
        self.assertIsNone(context["request"])
        self.assertIsNone(context["original_task"])
        self.assertIsNone(context["original_receipt"])

    def test_direct_original_capture_request_is_used_without_task_guess(self):
        state = self.memory.snapshot()
        original = state["captures"][self.capture_id]
        native_request = prepare_meta_query(self.request["query"])
        state["captures"][self.capture_id] = dict(original, request=native_request)
        state["query_tasks"] = {}; state["source_checks"] = {}
        context = meta_capture_context(state, self.observation_id)
        self.assertEqual(context["request"], native_request)
        self.assertEqual(context["request_basis"], "saved_capture_request")
        self.assertIsNone(context["original_task"])

    def test_receipt_must_own_observation_and_capture_with_same_original_identity(self):
        state = self.memory.snapshot()
        for field, value in (("capture_ids", []), ("observation_ids", [])):
            with self.subTest(field=field):
                altered = deepcopy(state)
                receipt = altered["source_checks"][self.receipt_id]
                receipt["envelope"][field] = value
                receipt["envelope_hash"] = digest(receipt["envelope"])
                self.assertEqual(meta_capture_context(altered, self.observation_id)["state"], "not_saved")
        altered = deepcopy(state)
        receipt = altered["source_checks"][self.receipt_id]
        receipt["envelope"]["acquisition_id"] = "foreign-original"
        receipt["envelope_hash"] = digest(receipt["envelope"])
        with self.assertRaisesRegex(SafetyError, "META_REQUEST_BINDING_INVALID"):
            meta_capture_context(altered, self.observation_id)

    def test_exact_shape_refuses_private_or_local_request_and_changed_date(self):
        payload = self.payload()
        e_contract(payload)
        for mutate in (lambda row: row.update(observed_at="2026-10-09T10:00:00+00:00"),
                lambda row: row["request"].update(token="private"),
                lambda row: row["request"].update(path="/tmp/receipt.json"),
                lambda row: row["capture"].update(table="jobs"),
                lambda row: row.update(extra="not part of the contract")):
            changed = deepcopy(payload)
            mutate(changed["items"][0]["observations"][0]["capture_context"])
            with self.assertRaisesRegex(SafetyError, "AI_FUNCTIONAL_SOURCE_CAPTURE_CONTEXT_INVALID"):
                e_contract(changed)

    def test_saved_old_v4_and_legacy_items_do_not_gain_mandatory_context(self):
        payload = self.payload()
        observation = payload["items"][0]["observations"][0]
        del observation["capture_context"]
        e_contract(payload)
        state = self.memory.snapshot()
        question = {"concept_id": "first", "unknown_ids": [self.observation_id]}
        self.assertNotIn("capture_context", _item(state, question, 20)["observations"][0])

    def test_harmless_task_progress_does_not_invalidate_original_request_proof(self):
        runner = Runner(); self.review(runner)
        old = self.memory.snapshot()["query_tasks"][self.task_id]
        self.memory.put("query_tasks", self.task_id, dict(old, reason="Updated local progress note."), previous=old)
        rows = validated_scoped_evidence(self.memory.snapshot(), "first", as_of=NOW)
        self.assertTrue(rows)
        capsule = rows[0]["job_proof"]["capture_contexts"][self.observation_id]
        self.assertEqual(capsule["original_task_snapshot"], old)
        self.assertEqual(digest(capsule["original_task_snapshot"]), capsule["context"]["original_task"]["hash"])
        self.review(runner)
        self.assertEqual(len(runner.calls), 1)

    def test_receipt_change_invalidates_accepted_context_without_fresh_collection(self):
        self.review()
        old = self.memory.snapshot()["source_checks"][self.receipt_id]
        self.memory.put("source_checks", self.receipt_id, dict(old, reason="Changed saved ownership receipt."), previous=old)
        self.assertFalse(validated_scoped_evidence(self.memory.snapshot(), "first", as_of=NOW))
        self.assertEqual(len(self.calls), 1)

    def test_original_request_change_between_job_and_commit_refuses_adoption(self):
        from product_radar import research_scope
        original_commit = research_scope._commit
        changed = []
        def race(memory, state, record, **kwargs):
            if not changed:
                old = memory.snapshot()["query_tasks"][self.task_id]
                request = dict(old["request"], query="a different product type")
                memory.put("query_tasks", self.task_id, dict(old, request=request, request_hash=digest(request)), previous=old)
                changed.append(True)
            return original_commit(memory, state, record, **kwargs)
        with patch.object(research_scope, "_commit", side_effect=race), self.assertRaises(SafetyError):
            self.review()
        self.assertFalse(validated_scoped_evidence(self.memory.snapshot(), "first", as_of=NOW))
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
