"""Company hint state reuse stays pure while original files remain live."""

from copy import deepcopy
import unittest
from unittest.mock import patch

import test_shared_company_pages as fixtures
import test_shared_company_programme as programme_fixtures
from test_supplier_route_acquisition import receipt

from product_radar.company_pages import (CompanyPagePending, company_source_hints,
                                        company_unstarted_continuation)
from product_radar.research_memory import key
from product_radar.source_coverage import source_access_context
from product_radar.storage import atomic_json
from product_radar.supplier_route_acquisition import create_supplier_handlers


class CompanyHintStateTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.SharedCompanyTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture, self.memory = fixture, fixture.memory

    def waiting_context(self, *, at=fixtures.LATER):
        helper = programme_fixtures.SharedCompanyProgrammeTests()
        helper.fixture, helper.memory, helper.root = self.fixture, self.memory, self.fixture.root
        _unseen, _healthy, task_id, _original, _record = helper.wait_company()
        state = self.memory.snapshot()
        return state, source_access_context(state, state["query_tasks"][task_id], as_of=at)

    def test_supplied_state_eliminates_all_snapshot_reads_without_changing_hints(self):
        state, context = self.waiting_context()
        before = deepcopy(state)
        due_hint, saved_hint = company_source_hints(self.memory)
        expected = (due_hint(context), saved_hint(context), company_unstarted_continuation(self.memory, context))
        self.assertTrue(expected[0]["ready"])
        self.assertIsNotNone(expected[2])
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("Unexpected journal replay")):
            actual = (due_hint(context, state=state), saved_hint(context, state=state),
                      company_unstarted_continuation(self.memory, context, state=state))
            earlier = dict(context, as_of=fixtures.NOW)
            self.assertFalse(due_hint(earlier, state=state)["ready"])
            self.assertIsNone(company_unstarted_continuation(self.memory, earlier, state=state))
        self.assertEqual(actual, expected)
        self.assertEqual(state, before)

    def test_factory_registers_same_function_objects_and_preserves_one_argument_defaults(self):
        state, context = self.waiting_context()
        handlers = create_supplier_handlers(self.memory, http_reader=fixtures.forbidden,
            page_reader=fixtures.forbidden, browser_reader=fixtures.forbidden, shared_acquisitions=True)
        company = handlers["company-pages"]
        self.assertIs(company["source_due_hint"], company["source_due_hint_from_state"])
        self.assertIs(company["source_saved_hint"], company["source_saved_hint_from_state"])
        with patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as fresh:
            self.assertTrue(company["source_due_hint"](context)["ready"])
            self.assertEqual(fresh.call_count, 1)
            company["source_saved_hint"](context)
            self.assertEqual(fresh.call_count, 2)
            self.assertIsNotNone(company["continue_unstarted"](context))
            self.assertEqual(fresh.call_count, 3)
        self.assertEqual(state, self.memory.snapshot())

    def test_dispatch_continuation_keeps_fresh_authority_instead_of_saved_hint_state(self):
        state, context = self.waiting_context()
        handlers = create_supplier_handlers(self.memory, shared_acquisitions=True)
        changed = deepcopy(state)
        changed["query_tasks"][context["task_id"]]["reconcile_required"] = False
        with patch.object(self.memory, "snapshot", return_value=changed) as fresh:
            self.assertIsNotNone(company_unstarted_continuation(self.memory, context, state=state))
            fresh.assert_not_called()
            self.assertIsNone(handlers["company-pages"]["continue_unstarted"](context))
            fresh.assert_called_once_with()

    def test_same_supplied_state_still_detects_new_native_original_files(self):
        http = fixtures.OwnedReader(text="")
        browser_calls = []
        def browser(item):
            browser_calls.append(deepcopy(item))
            value = receipt(item, body="", status=200)
            value.update(state="unavailable", owned_process_exited=False)
            return value
        with self.assertRaises(CompanyPagePending):
            self.fixture.fetcher(http, browser_reader=browser)({"id": "pending-page", "url": fixtures.URL})
        state = self.memory.snapshot()
        original_state = deepcopy(state)
        leaf_id = next(rid for rid, row in state["batch_runs"].items()
            if row.get("kind") == "source-acquisition-owner-v1"
            and key("supplier-company-read", rid) == browser_calls[0]["id"])
        native = self.memory.root / "supplier_routes" / "company_shared" / leaf_id / "native"
        context = {"request": {"candidate_urls": {"paper": [fixtures.URL]}},
                   "as_of": fixtures.LATER, "reconciling": True}
        due_hint, saved_hint = company_source_hints(self.memory)
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("Unexpected journal replay")):
            before_saved = saved_hint(context, state=state)
            self.assertFalse(due_hint(context, state=state)["ready"])
            atomic_json(native / "native_result.json", {
                "navigation_started": True, "navigation_started_at": fixtures.NOW,
                "completed_at": fixtures.NOW, "owned_process_exited": True,
                "state": "captured", "navigation_status": 200,
                "snapshot": {"url": fixtures.URL, "html": "<p>Factory wrapping paper.</p>",
                             "text": "Factory wrapping paper.", "links": []}})
            atomic_json(native / "native_dispatch.json", {"url": fixtures.URL,
                "navigation_started": True, "navigation_started_at": fixtures.NOW})
            after_saved = saved_hint(context, state=state)
            after_due = due_hint(context, state=state)
        self.assertNotEqual(before_saved, after_saved)
        self.assertTrue(after_due["local_saved"])
        self.assertTrue(after_due["ready"])
        self.assertEqual(state, original_state)
        self.assertEqual(state, self.memory.snapshot())
        self.assertEqual((len(http.calls), len(browser_calls)), (1, 1))


if __name__ == "__main__":
    unittest.main()
