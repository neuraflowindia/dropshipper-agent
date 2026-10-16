"""Research trace loading: chain-checked replay, run logs, AI folders, spend
watch, meter start and platform reads, all read-only.

Synthetic data only (tests/trace_fixture.py); no real research records.
"""

import json
from pathlib import Path
import tempfile
import unittest

import trace_fixture as fx
from product_radar import trace_records as records
from product_radar.models import SafetyError


class TraceRecordsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "store"
        self.memory = fx.build(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_replay_rebuilds_state_and_time_line(self):
        state, timeline, facts = records.replay_events(self.memory / "events")
        self.assertEqual(facts, {"events": 8, "first_event_at": fx.T0, "last_event_at": fx.T4})
        self.assertEqual(state["query_tasks"]["task-1"]["status"], "acquired")
        self.assertEqual(timeline.first_at("query_tasks", "task-1"), fx.T1)
        self.assertEqual(timeline.last[("query_tasks", "task-1")], fx.T3)
        self.assertEqual(timeline.changes[("query_tasks", "task-1")], 3)
        statuses = [summary["status"] for _, _, summary in timeline.history[("query_tasks", "task-1")]]
        self.assertEqual(statuses, ["prepared", "dispatching", "acquired"])
        self.assertEqual(timeline.history[("concepts", fx.A)][0][2]["label"], "Folding test rack")
        self.assertEqual(timeline.first_at("concepts", "missing"), None)

    def test_replay_matches_the_programmes_own_read(self):
        from product_radar.research_memory import ResearchMemory
        state, _, _ = records.replay_events(self.memory / "events")
        self.assertEqual(state, ResearchMemory(self.memory, None).snapshot())

    def test_a_change_the_programme_would_refuse_stops_the_trace(self):
        folder = Path(self.temp.name) / "rewrite" / "events"
        capture = {"source": "meta", "reference": "x"}
        fx.write_events(folder, [(fx.T1, [("captures", "cap-1", capture)]),
                                 (fx.T2, [("captures", "cap-1", {**capture, "source": "changed"})])])
        with self.assertRaisesRegex(records.TraceError, "00000002.json"):
            records.replay_events(folder)

    def test_unchanged_summary_is_kept_once(self):
        timeline = records.Timeline()
        timeline.add("concepts", "c", {"status": "provisional"}, fx.T1, 1)
        timeline.add("concepts", "c", {"status": "provisional"}, fx.T2, 2)
        timeline.add("concepts", "c", {"status": "merged"}, fx.T3, 3)
        self.assertEqual([row[1] for row in timeline.history[("concepts", "c")]], [1, 3])
        self.assertEqual(timeline.changes[("concepts", "c")], 3)

    def test_broken_or_reordered_chain_stops_the_trace(self):
        path = self.memory / "events" / "00000003.json"
        event = json.loads(path.read_text())
        event["at"] = fx.T4
        path.write_text(json.dumps(event))
        with self.assertRaisesRegex(records.TraceError, "TRACE_EVENTS_CHAIN_BROKEN:00000003.json"):
            records.replay_events(self.memory / "events")
        (self.memory / "events" / "00000003.json").unlink()
        with self.assertRaisesRegex(records.TraceError, "CHAIN_BROKEN:00000004.json"):
            records.replay_events(self.memory / "events")

    def test_unknown_table_is_refused(self):
        folder = Path(self.temp.name) / "bad" / "events"
        fx.write_events(folder, [(fx.T1, [("not_a_table", "x", {"a": 1})])])
        with self.assertRaises(records.TraceError):
            records.replay_events(folder)

    def test_missing_events_folder_is_an_empty_history(self):
        state, timeline, facts = records.replay_events(Path(self.temp.name) / "none")
        self.assertEqual(facts["events"], 0)
        self.assertEqual(state["concepts"], {})
        self.assertEqual(timeline.first, {})

    def test_run_logs_include_rotated_files_and_skip_other_files(self):
        rows, unreadable = records.load_run_rows(self.root / "data" / "operations" / "research-runs")
        self.assertEqual(len(rows), len(fx.run_rows()))
        self.assertEqual(unreadable, 2)
        self.assertEqual(rows[0]["at"], fx.T0)
        self.assertEqual(records.load_run_rows(Path(self.temp.name) / "none"), ([], 0))

    def test_ai_job_folders_are_read_and_bad_files_marked(self):
        jobs = {job["folder"]: job for job in records.load_ai_jobs(self.memory / "ai_jobs")}
        self.assertIn("result", jobs["job-a"]["present"])
        self.assertEqual(len(jobs["job-r"]["attempts"]), 2)        # original + retry-1
        self.assertEqual(len(jobs["job-d"]["attempts"]), 1)        # retry recorded, its folder never written
        self.assertEqual(jobs["job-h"]["intent"], {"unreadable": True})
        self.assertEqual(jobs["job-g"]["other_files"], ["supplier_scoped_reference_map.json"])
        self.assertEqual(records.load_ai_jobs(Path(self.temp.name) / "none"), [])

    def test_spend_watch_readings_alerts_and_damaged_state(self):
        spend = records.load_spend_watch(self.memory / "spend_watch")
        self.assertEqual(spend["state"]["items"]["ai-week"]["percent"], 100)
        self.assertEqual([row["at"] for row in spend["alerts"]], [fx.T2, fx.T3])
        self.assertEqual(records.load_spend_watch(Path(self.temp.name) / "none"), {"state": None, "alerts": []})
        state_path = self.memory / "spend_watch" / "state.json"
        wrapper = json.loads(state_path.read_text())
        wrapper["hash"] = "0" * 64
        state_path.write_text(json.dumps(wrapper))
        with self.assertRaisesRegex(records.TraceError, "SPEND_WATCH"):
            records.load_spend_watch(self.memory / "spend_watch")

    def test_meter_start(self):
        self.assertEqual(records.load_baseline(self.root)["started_at"], fx.START)
        self.assertIsNone(records.load_baseline(Path(self.temp.name) / "none"))
        path = self.root / "data" / "operations" / "meter-baseline.json"
        wrapper = json.loads(path.read_text())
        wrapper["baseline"]["reason"] = "changed"
        path.write_text(json.dumps(wrapper))
        with self.assertRaisesRegex(records.TraceError, "METER_START"):
            records.load_baseline(self.root)

    def test_platform_reads(self):
        platforms = records.load_platform_reads(self.memory)
        self.assertEqual([row["state"] for row in platforms["deodap"]["reads"]], ["read", "failed"])
        self.assertEqual(platforms["dropdash"]["reads"][0]["state"], "unreadable")
        self.assertEqual(platforms["dropdash"]["refresh"]["wait"]["reason"], "OWNER_SIGN_IN_REQUIRED")
        self.assertIn("unreadable", platforms["baapstore"]["refresh"])

    def test_store_and_data_root(self):
        store = records.load_store(self.root, clock=iter([1.0, 3.5]).__next__)
        self.assertEqual(store.load_seconds, 2.5)
        self.assertEqual(store.first_at("concepts", fx.A), fx.T1)
        self.assertEqual(records.default_since(store), fx.START)
        store.baseline = None
        self.assertEqual(records.default_since(store), fx.T0)
        self.assertEqual(records.resolve_data_root(Path("/unused"), self.root), self.root.absolute())
        with self.assertRaisesRegex(records.TraceError, "NOT_FOUND"):
            records.resolve_data_root(Path("/unused"), Path(self.temp.name) / "none")
        # Without a storage setting the project folder itself holds data/.
        self.assertEqual(records.resolve_data_root(self.root), self.root.absolute())

    def test_time_helpers(self):
        self.assertEqual(records.day_of("2026-10-16T19:00:00+00:00"), "2026-10-17")
        self.assertEqual(records.day_of("2026-10-16T18:00:00+00:00"), "2026-10-16")
        self.assertEqual(records.seconds_between(fx.T1, fx.T2), 30.0)
        self.assertEqual(records.seconds_between(fx.T2, fx.T1), 0.0)
        self.assertIsNone(records.seconds_between(None, fx.T2))
        self.assertIsNone(records.seconds_between("bad", fx.T2))
        self.assertTrue(records.at_or_after(fx.T1, None))
        self.assertTrue(records.at_or_after(fx.T1, fx.T1))
        self.assertFalse(records.at_or_after(fx.T0, fx.T1))
        self.assertFalse(records.at_or_after("", fx.T1))
        self.assertEqual(records.day_span(fx.T1, fx.T4), ["2026-10-16", "2026-10-17"])
        self.assertEqual(records.day_span(None, fx.T4), [])

    def test_summary_keeps_small_codes_only(self):
        summary = records.summarise("query_tasks", {
            "kind": "k", "status": "s", "reason": "r" * 500, "tries": 3, "accepted": True, "page_text": "long",
            "binding": {"route": "meta-ads"}, "progress": {"acquisition": "prepared", "reason": "WAIT",
                                                          "interpretation": "not_started"},
            "targets": {"b": 1, "a": 2}, "target_results": {"a": {"state": "done"}, "b": "bad"}})
        self.assertEqual(len(summary["reason"]), records.SUMMARY_TEXT_LIMIT)
        self.assertNotIn("page_text", summary)
        self.assertEqual(summary["route"], "meta-ads")
        self.assertEqual(summary["progress_reason"], "WAIT")
        self.assertEqual(summary["targets"], ["a", "b"])
        self.assertEqual(summary["target_states"], {"a": "done"})
        self.assertTrue(summary["accepted"])
        jobs = records.summarise("jobs", {"request": {"role": "A", "prompt_version": "p", "payload": {}}})
        self.assertEqual(jobs, {"role": "A", "prompt_version": "p"})

    def test_trace_error_is_a_safety_error(self):
        self.assertTrue(issubclass(records.TraceError, SafetyError))


if __name__ == "__main__":
    unittest.main()
