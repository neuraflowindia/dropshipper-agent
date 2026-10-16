"""Research trace, AI use per step: outcomes, tokens and time from synthetic
AI job folders, plus waits and allowance readings."""

from pathlib import Path
import tempfile
import unittest

import trace_fixture as fx
from product_radar.spend_watch_ai import GUARD_CODES
from product_radar.trace_ai import NOT_RECORDED, ai_report, classify
from product_radar.trace_records import load_store


class TraceAiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name) / "store"
        fx.build(root)
        cls.store = load_store(root)
        cls.report = ai_report(cls.store, fx.START)
        cls.steps = {row["step"]: row for row in cls.report["steps"]}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_each_folder_outcome(self):
        outcomes = {job["folder"]: classify(job)["outcome"] for job in self.store.ai_jobs}
        self.assertEqual(outcomes, {"job-a": "finished", "job-b": "refused", "job-c": "finished", "job-d": "failed",
                                    "job-e": "no_answer_saved", "job-f": "derived_no_ai_call", "job-g": "not_started",
                                    "job-h": "finished", "job-old": "finished", "quote-repair-1": "repair_claim"})
        job_a = classify(next(job for job in self.store.ai_jobs if job["folder"] == "job-a"))
        self.assertEqual((job_a["wall_seconds"], job_a["input_tokens"], job_a["cached_input_tokens"]), (30.0, 1000, 100))
        self.assertEqual((job_a["purpose"], job_a["items"], job_a["exit_code"]), ("A", 2, 0))
        job_d = classify(next(job for job in self.store.ai_jobs if job["folder"] == "job-d"))
        self.assertEqual((job_d["retries"], job_d["failure_code"], job_d["wall_seconds"]),
                         (1, "AI_INTERRUPTED_REVIEW_SAVED_JOB", 7200.0))

    def test_per_step_and_totals(self):
        self.assertEqual(set(self.steps), {"research-abe-v1", "research-b-functional-v2", "research-e-fit10-functional-v5",
                                           "research-e-source-relevance-v2", "research-f-v4"})
        self.assertEqual(self.steps["research-b-functional-v2"]["outcomes"], {"refused": 1})
        self.assertEqual(self.steps["research-b-functional-v2"]["failures"], {GUARD_CODES[0]: 1})
        self.assertIn("Name the products", self.steps["research-abe-v1"]["label"])
        total = self.report["total"]
        self.assertEqual(total["jobs"], 5)
        self.assertEqual(total["outcomes"], {"finished": 2, "failed": 1, "refused": 1, "no_answer_saved": 1})
        self.assertEqual((total["input_tokens"], total["cached_input_tokens"], total["output_tokens"]), (2000, 200, 100))
        self.assertEqual((total["total_wall_seconds"], total["median_wall_seconds"], total["timed_jobs"]), (7250.0, 30.0, 3))
        self.assertEqual(self.report["other_ai_folders"], {"derived_no_ai_call": 1, "not_started": 1, "repair_claim": 1})

    def test_by_day_waits_and_allowance(self):
        self.assertEqual({row["day"]: row["jobs"] for row in self.report["by_day"]}, {"2026-10-16": 4, "2026-10-17": 1})
        self.assertEqual(len(self.report["by_day_and_step"]), 5)
        self.assertEqual(self.report["round_waits_for_ai"], {"WORK_TURN_MODEL_LIMIT": 1})
        stops = {(row["stage"], row["reason"], row["later"]): row["count"]
                 for row in self.report["ai_steps_that_could_not_start"]}
        self.assertEqual(len(stops), 8)
        self.assertEqual(stops[("photos", "AI_PHOTO_CHECK_WAITING", "unknown")], 1)
        self.assertEqual(stops[("identity", "AI_PLAN_LIMIT_CREDITS_PROTECTED", "done_later")], 1)
        self.assertEqual(self.report["allowance_readings_latest"][0]["percent"], 100)
        self.assertEqual([row["percent"] for row in self.report["allowance_alerts"]], [75, 100])
        self.assertEqual(self.report["allowance_waits_now"][0]["route"], "ai")
        self.assertEqual((self.report["per_job_allowance"], self.report["notes"]), ("not_recorded", [NOT_RECORDED]))

    def test_period_and_no_spend_watch(self):
        self.store.spend, saved = {"state": None, "alerts": []}, self.store.spend
        try:
            later = ai_report(self.store, fx.T4)
        finally:
            self.store.spend = saved
        self.assertEqual(later["total"]["jobs"], 1)
        self.assertEqual((later["allowance_readings_latest"], later["allowance_waits_now"]), ([], []))
        self.assertIsNone(ai_report(self.store, "2026-10-18T00:00:00+00:00")["total"]["median_wall_seconds"])


if __name__ == "__main__":
    unittest.main()
