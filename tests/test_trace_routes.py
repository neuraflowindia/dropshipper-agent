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
        # One line that the meter counts both as a failure and as a refusal (a fresh read
        # that met the sign-in wall).
        cls.store.run_rows.append({"at": fx.T3, "event": "source_outcome", "route": "indiamart", "state": "blocked",
                                   "actual_attempt": True, "fresh_read": True, "error_type": "AccessRefused",
                                   "reason": "OWNER_SIGN_IN_REQUIRED"})
        # Platform page visits: a refused feed (a block) and a sign-in wall (a wait).
        cls.store.run_rows.append({"at": fx.T3, "event": "platform_read", "route": "deodap", "state": "failed",
                                   "reason": "DROPSHIP_FEED_REFUSED", "http_status": 429})
        cls.store.run_rows.append({"at": fx.T3, "event": "platform_read", "route": "dropdash", "state": "failed",
                                   "reason": "OWNER_SIGN_IN_REQUIRED", "signed_in": False})
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
        # An older "blocked" line without the fresh-read fact proves no refusal: a wait (R18a meter rule).
        self.assertEqual(trends["access_blocks"], {})
        self.assertEqual(trends["waits"], {"blocked": 2, "SIGNED_IN_READ_SPACING": 1})
        self.assertEqual(trends["website"]["reads_timed"], 0)
        pages = self.routes["company-pages"]["run_logs"]
        self.assertEqual((pages["failures"], pages["reused_saved_failures"]), ({"SOURCE_ACTION_TIMEOUT": 1}, 1))
        self.assertNotIn("records", self.routes["company-pages"])
        both = self.routes["indiamart"]["run_logs"]
        self.assertEqual((both["failures"], both["access_blocks"], both["waits"]),
                         ({"OWNER_SIGN_IN_REQUIRED": 1}, {"OWNER_SIGN_IN_REQUIRED": 1}, {}))
        self.assertEqual(self.routes["deodap"]["run_logs"]["access_blocks"], {"DROPSHIP_FEED_REFUSED": 1})
        self.assertEqual(self.routes["dropdash"]["run_logs"]["waits"], {"OWNER_SIGN_IN_REQUIRED": 1})

    def test_the_trace_and_the_owner_meter_count_the_same_rows_alike(self):
        import json
        from product_radar.run_log_summary import summarise_lines
        from product_radar.trace_records import at_or_after
        rows = [row for row in self.store.run_rows if row["event"] in ("source_outcome", "platform_read")
                and at_or_after(row["at"], fx.START)]
        meter = summarise_lines([json.dumps(row) for row in rows], None)[0]
        logs = [row["run_logs"] for row in self.routes.values() if "run_logs" in row]
        totals = {name: sum(sum(item[name].values()) for item in logs) for name in ("failures", "access_blocks", "waits")}
        self.assertEqual((totals["failures"], totals["access_blocks"], totals["waits"]),
                         (meter["failures"], meter["access_blocks"], meter["waits"]))
        self.assertEqual(sum(item["reused_saved_failures"] for item in logs), meter["reused_saved_failures"])

    def test_catalogue_platforms(self):
        self.assertIn("saved copy", self.routes["deodap"]["note"])
        deodap = self.report["platforms"]["deodap"]
        self.assertEqual((deodap["reads"], dict(deodap["states"])), (2, {"read": 1, "failed": 1}))
        self.assertEqual(dict(deodap["failures"]), {"DROPSHIP_PAGE_LAYOUT_UNRECOGNISED": 1})
        self.assertEqual((deodap["median_seconds"], deodap["last_success_at"]), (15.0, fx.T1))
        dropdash = self.report["platforms"]["dropdash"]
        self.assertEqual((dropdash["reads"], dict(dropdash["states"]), dropdash["wait_now"], dropdash["failures_in_a_row"]),
                         (1, {"unreadable": 1}, "OWNER_SIGN_IN_REQUIRED", 2))
        self.assertIsNone(self.report["platforms"]["baapstore"]["wait_now"])

    def test_period_filter(self):
        later = routes_report(self.store, fx.T4)
        self.assertEqual(later["routes"], [])
        self.assertEqual(later["platforms"]["deodap"]["reads"], 0)
        self.assertEqual(later["platforms"]["dropdash"]["reads"], 1)   # an unreadable read has no time: always shown


if __name__ == "__main__":
    unittest.main()
