"""Continue repaired, never-submitted work without reposting paid attempts."""

import unittest
from unittest.mock import patch

import test_research_batch as helpers
from product_radar.models import SafetyError
from product_radar.research_batch import execute_plan, prepare_continuation


class ContinuationTests(unittest.TestCase):
    setUp = helpers.ResearchBatchTests.setUp
    prepare = helpers.ResearchBatchTests.prepare
    execute = helpers.ResearchBatchTests.execute

    def test_known_failed_paid_attempt_is_not_added_to_continuation(self):
        prepared = self.prepare(self.plan, self.plan["actions"][0])
        self.behaviors[prepared.attempt_id] = "no_output"
        self.execute(action_limit=1)
        self.assertIsNone(prepare_continuation(self.memory, self.plan["id"], reason="Source checked."))
        self.assertEqual(len(self.launches), 1)

    def test_uncertain_attempt_is_excluded_and_later_unsubmitted_work_keeps_binding(self):
        prepared = self.prepare(self.plan, self.plan["actions"][0])
        self.behaviors[prepared.attempt_id] = "unknown"
        original = self.execute(action_limit=2)
        self.assertEqual(len(self.launches), 1)
        continuation = prepare_continuation(self.memory, self.plan["id"], reason="Saved submission recovery is still required.")
        self.assertEqual([a["action_id"] for a in continuation["actions"]], [self.plan["actions"][1]["action_id"]])
        self.assertEqual(continuation["continuation_of"], self.plan["id"])
        # The unresolved original attempt continues to prevent new charges.
        result = execute_plan(self.memory, continuation["id"], prepare_action=self.prepare,
                              controller_factory=lambda: helpers.FixtureController(self), runner=self.runner)
        self.assertTrue(result["paid_dispatch_stopped"])
        self.assertEqual(len(self.launches), 1)
        self.assertEqual(original["actions"][0]["attempt_id"], prepared.attempt_id)

    def test_reason_and_existing_execution_are_required(self):
        for reason in ("", None):
            with self.assertRaisesRegex(SafetyError, "CONTINUATION_REASON"):
                prepare_continuation(self.memory, self.plan["id"], reason=reason)
        with self.assertRaisesRegex(SafetyError, "EXECUTED_PLAN"):
            prepare_continuation(self.memory, self.plan["id"], reason="Checked.")

    def test_repaired_preflight_continues_exact_unsubmitted_binding_once(self):
        with patch.object(helpers.FixtureController, "preflight", side_effect=SafetyError("SAVED_FILE_UNAVAILABLE")):
            original = self.execute(action_limit=1)
        entry = original["actions"][0]
        self.assertEqual(entry["status"], "blocked")
        self.assertEqual(self.launches, [])
        continuation = prepare_continuation(self.memory, self.plan["id"], reason="Recovered the missing saved source receipt.")
        first = execute_plan(self.memory, continuation["id"], prepare_action=self.prepare,
                             controller_factory=lambda: helpers.FixtureController(self), runner=self.runner)
        self.assertEqual(first["actions"][0]["attempt_id"], entry["attempt_id"])
        self.assertEqual(first["actions"][0]["task_id"], entry["task_id"])
        self.assertEqual(first["actions"][0]["status"], "captured")
        self.assertEqual(len(self.launches), 1)
        replay = execute_plan(self.memory, continuation["id"], prepare_action=self.prepare,
                              controller_factory=lambda: helpers.FixtureController(self), runner=self.runner)
        self.assertTrue(replay["replay"])
        self.assertEqual(len(self.launches), 1)
        self.assertIsNone(prepare_continuation(self.memory, continuation["id"], reason="Already complete."))
