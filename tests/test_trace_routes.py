"""Research trace, per website: reads, answers, reuse, failures, waits,
refusals and time per read, from synthetic records only."""

from pathlib import Path
import tempfile
import unittest

import trace_fixture as fx
from product_radar.trace_records import load_store
from product_radar.trace_routes import routes_report


class TraceRoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name) / "store"
        fx.build(root)
        cls.store = load_store(root)
        cls.store.run_rows.append({"at": fx.T3, "event": "source_outcome", "route": "deodap", "state": "acquired",
                                   "actual_attempt": True, "timings": {"handler": 0.01}})
        cls.report = routes_report(cls.store, fx.START)
        cls.routes = {row["route"]: row for row in cls.report["routes"]}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_saved_reads_per_website(self):
        amazon = self.routes["amazon-search"]["records"]
        self.assertEqual((amazon["planned"], amazon["sent"], amazon["answers_saved"]), (1, 1, 1))
        self.assertEqual(amazon["answers_by_result"], {"usable": 1})
        self.assertEqual((amazon["median_seconds"], amazon["last_answer_at"]), (30.0, fx.T2))
        meta = self.routes["meta-ads"]["records"]
        self.assertEqual(meta["lanes"], {"discovery": 1})
        self.assertEqual((meta["answers_by_result"], meta["average_seconds"]), ({"empty": 1}, 7200.0))

    def test_waiting_refused_and_unusable_reads(self):
        trends = self.routes["google-trends"]["records"]
        self.assertEqual((trends["planned"], trends["sent"], trends["not_sent_yet"]), (2, 1, 1))
        self.assertEqual(trends["unusable_answers_by_reason"], {"blocked: TRENDS_MINIMISED_NOT_RENDERED": 1})
        self.assertEqual((trends["refusals"], trends["refusals_with_challenge_page"]), (1, 1))
        self.assertIsNone(trends["last_answer_at"])
        self.assertEqual(self.routes["unknown"]["records"]["refusals"], 1)

    def test_run_log_outcomes_follow_the_meter_rules(self):
        amazon = self.routes["amazon-search"]["run_logs"]
        self.assertEqual((amazon["attempted"], amazon["fresh_answers"], amazon["reused_saved_answers"]), (2, 2, 1))
        self.assertEqual(amazon["waits"], {"WORK_TURN_SOURCE_LIMIT": 1})
        self.assertEqual(amazon["website"], {"reads_timed": 2, "average_seconds": 5.0, "median_seconds": 5.0})
        self.assertEqual(amazon["last_success_at"], fx.T1)
        trends = self.routes["google-trends"]["run_logs"]
        self.assertEqual(trends["access_blocks"], {"blocked": 1})
        self.assertEqual(trends["waits"], {"blocked": 1, "SIGNED_IN_READ_SPACING": 1})
        self.assertEqual(trends["website"]["reads_timed"], 0)
        pages = self.routes["company-pages"]["run_logs"]
        self.assertEqual((pages["failures"], pages["reused_saved_failures"]), ({"SOURCE_ACTION_TIMEOUT": 1}, 1))
        self.assertNotIn("records", self.routes["company-pages"])

    def test_catalogue_platforms(self):
        self.assertIn("saved copy", self.routes["deodap"]["note"])
        deodap = self.report["platforms"]["deodap"]
        self.assertEqual((deodap["reads"], dict(deodap["states"])), (2, {"read": 1, "failed": 1}))
        self.assertEqual(dict(deodap["failures"]), {"DROPSHIP_PAGE_LAYOUT_UNRECOGNISED": 1})
        self.assertEqual((deodap["median_seconds"], deodap["last_success_at"]), (15.0, fx.T1))
        dropdash = self.report["platforms"]["dropdash"]
        self.assertEqual((dropdash["reads"], dropdash["wait_now"], dropdash["failures_in_a_row"]),
                         (0, "OWNER_SIGN_IN_REQUIRED", 2))
        self.assertIsNone(self.report["platforms"]["baapstore"]["wait_now"])

    def test_period_filter(self):
        later = routes_report(self.store, fx.T4)
        self.assertEqual(later["routes"], [])
        self.assertEqual(later["platforms"]["deodap"]["reads"], 0)


if __name__ == "__main__":
    unittest.main()
