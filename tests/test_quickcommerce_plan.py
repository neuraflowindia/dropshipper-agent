"""Which quick-commerce checks are worth a credit (Plan R10 W5).

Places, admission (checklist pass and Low/Medium crowding) at planning and
send time, the one-phrase rule, the Mumbai-first order and the 30-day repeat
rule. Synthetic records only; nothing is sent.
"""

from contextlib import redirect_stderr
import importlib
import importlib.util
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from product_radar import quickcommerce_plan as plan
from product_radar.models import SafetyError
from product_radar.research_memory import ResearchMemory
from product_radar.source_coverage import prepare_all_source_tasks, product_source_request_builder, source_coverage
from product_radar.storage import Journal
import test_source_coverage as coverage_fixtures

NOW = coverage_fixtures.NOW                 # 2026-10-08
WEEK_LATER = "2026-10-15T10:00:00+00:00"
MONTH_LATER = "2026-11-09T10:00:00+00:00"   # past the 30-day recheck
MUMBAI = {"name": "Mumbai", "lat": "19.0760", "lon": "72.8777"}
DELHI = {"name": "Delhi NCR", "lat": "28.6139", "lon": "77.2090"}
BENGALURU = {"name": "Bengaluru", "lat": "12.9716", "lon": "77.5946"}
PLACES = {"places": [MUMBAI, DELHI, BENGALURU], "recheck_days": 30}
APPS = ("blinkit", "zepto", "instamart")
HELD = ("QUICKCOMMERCE_RESERVED_CHECKLIST", "QUICKCOMMERCE_RESERVED_CROWDING_HIGH",
        "QUICKCOMMERCE_RESERVED_CROWDING_NOT_CHECKED")


class SettingsTests(unittest.TestCase):
    def good(self):
        return json.loads(plan.LOCATIONS.read_text())

    def test_the_three_reference_points_mumbai_first_and_a_30_day_recheck(self):
        self.assertEqual(plan.load_settings(), PLACES)

    def test_invalid_settings_are_refused(self):
        good = self.good()
        bad = [dict(good, kind="other"), dict(good, schema_version=2), dict(good, locations=[]),
               dict(good, extra=1), dict(good, note=7), dict(good, recheck_days=6), dict(good, recheck_days="30"),
               {key: value for key, value in good.items() if key != "recheck_days"},
               dict(good, locations=[dict(MUMBAI, lat=19.076)] + good["locations"][1:]),
               dict(good, locations=[dict(MUMBAI, lat="91")]), dict(good, locations=[dict(MUMBAI, pin="400001")]),
               dict(good, locations=[MUMBAI, dict(MUMBAI, name="mumbai ")]),
               dict(good, locations=[MUMBAI, dict(DELHI, lat=MUMBAI["lat"], lon=MUMBAI["lon"])]),
               dict(good, locations=[dict(MUMBAI, name="Place " + str(i), lat=str(10 + i)) for i in range(6)]),
               dict(good, locations=["Mumbai"]), [], {"locations": None}]
        for value in bad:
            with self.subTest(value=value), self.assertRaisesRegex(SafetyError, "QUICKCOMMERCE_LOCATIONS_INVALID"):
                plan.validate_settings(value)

    def test_missing_or_unreadable_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(SafetyError, "QUICKCOMMERCE_LOCATIONS_INVALID"):
                plan.load_settings(Path(folder) / "missing.json")


class AdmissionTests(unittest.TestCase):
    def hold(self, *, passing=True, level="low"):
        with patch("product_radar.research_flow.current_assessment_fit", return_value={"eligible": passing}), \
             patch("product_radar.nearly_ready.passing_checklist", return_value=passing), \
             patch("product_radar.crowding.crowding_for", return_value={"level": level}):
            return plan.admission_hold({}, "sink")

    def test_only_passing_types_with_low_or_medium_crowding_are_admitted(self):
        self.assertIsNone(self.hold(level="low"))
        self.assertIsNone(self.hold(level="medium"))
        self.assertEqual(self.hold(level="high"), "QUICKCOMMERCE_RESERVED_CROWDING_HIGH")
        self.assertEqual(self.hold(level="not_checked"), "QUICKCOMMERCE_RESERVED_CROWDING_NOT_CHECKED")
        self.assertEqual(self.hold(passing=False, level="low"), "QUICKCOMMERCE_RESERVED_CHECKLIST")

    def test_real_records_without_a_checklist_are_reserved(self):
        with tempfile.TemporaryDirectory() as folder:
            memory = ResearchMemory(Path(folder) / "r", Journal(Path(folder) / "f"), evidence_type="synthetic")
            memory.put("concepts", "sink", {"status": "active", "description": {"label": "Sink caddy"}})
            self.assertEqual(plan.admission_hold(memory.snapshot(), "sink"), "QUICKCOMMERCE_RESERVED_CHECKLIST")


class FirstPlaceTests(unittest.TestCase):
    def state(self, location, target="sink"):
        # The type's own Zepto question returned obs-1 (and, oddly, a Meta row).
        return {"observations": {"obs-1": {"source": "zepto", "facts": {"location": location}},
                                 "obs-meta": {"source": "meta", "facts": {}}},
                "query_tasks": {"task": {"route": "zepto", "targets": {target: "hash"}, "receipt_refs": ["r1", "missing"]},
                                "other": {"route": "meta-ads", "targets": {"sink": "hash"}, "receipt_refs": ["r1"]}},
                "source_checks": {"r1": {"envelope": {"observation_ids": ["obs-1", "obs-meta", "missing"]}}}}

    def review(self, ref="obs-1", **changes):
        from product_radar.source_interpretation import FUNCTIONAL_MATCH_METHOD
        row = {"method": FUNCTIONAL_MATCH_METHOD, "route": "zepto", "scope": "narrow_type", "admissibility": "accepted",
               "references": [{"id": ref, "table": "observations"}]}
        return dict(row, **changes)

    def relevant(self, state, rows, match=True):
        with patch("product_radar.research_scope.validated_scoped_evidence", return_value=rows), \
             patch("product_radar.market_validation._functional_match", return_value=match):
            return plan.first_place_relevant(state, "sink", MUMBAI, as_of=NOW)

    def test_only_an_accepted_narrow_match_on_a_first_place_row_counts(self):
        self.assertTrue(self.relevant(self.state(MUMBAI), [self.review()]))
        self.assertFalse(self.relevant(self.state(DELHI), [self.review()]))
        self.assertFalse(self.relevant(self.state(MUMBAI), [self.review()], match=False))
        for changes in ({"scope": "broader_family"}, {"admissibility": "not_accepted"}, {"route": "meta-ads"},
                        {"method": "operator_source_review"}, {"references": [{"id": "obs-1", "table": "trends_evidence"}]}):
            with self.subTest(changes=changes):
                self.assertFalse(self.relevant(self.state(MUMBAI), [self.review(**changes)]))
        self.assertFalse(self.relevant(self.state(MUMBAI), [self.review("obs-meta")]))
        self.assertFalse(self.relevant(self.state(MUMBAI), [self.review("missing")]))
        self.assertFalse(self.relevant(self.state(MUMBAI), []))

    def test_rows_from_other_types_questions_skip_the_costly_review_read(self):
        with patch("product_radar.research_scope.validated_scoped_evidence") as scoped:
            self.assertFalse(plan.first_place_relevant(self.state(MUMBAI, target="other"), "sink", MUMBAI, as_of=NOW))
            self.assertFalse(plan.first_place_relevant(self.state(DELHI), "sink", MUMBAI, as_of=NOW))
        scoped.assert_not_called()

    def test_pass_cache_answers_each_type_once(self):
        cache = {}
        with patch("product_radar.quickcommerce_plan.admission_hold", return_value=None) as hold, \
             patch("product_radar.quickcommerce_plan.first_place_relevant", return_value=True) as first:
            for place in (DELHI, BENGALURU, DELHI):
                self.assertIsNone(plan.paid_question_hold({}, ["sink"], place, as_of=NOW, settings=PLACES, cache=cache))
        self.assertEqual((hold.call_count, first.call_count), (1, 1))


class PaidQuestionHoldTests(unittest.TestCase):
    """Admission again just before a paid call."""

    def hold(self, location, holds, relevant=False, concept_ids=("sink", "other")):
        with patch("product_radar.quickcommerce_plan.admission_hold", side_effect=lambda state, cid: holds.get(cid)), \
             patch("product_radar.quickcommerce_plan.first_place_relevant", return_value=relevant) as first:
            return plan.paid_question_hold({}, list(concept_ids), location, as_of=NOW, settings=PLACES), first

    def test_any_admitted_target_lets_the_question_run(self):
        self.assertIsNone(self.hold(MUMBAI, {"sink": "QUICKCOMMERCE_RESERVED_CHECKLIST"})[0])
        held, first = self.hold(MUMBAI, {"sink": HELD[1], "other": HELD[0]})
        self.assertEqual(held, HELD[1]); first.assert_not_called()

    def test_a_later_place_also_needs_the_relevant_first_place_result(self):
        self.assertEqual(self.hold(DELHI, {})[0], "QUICKCOMMERCE_RESERVED_FIRST_PLACE")
        self.assertIsNone(self.hold(DELHI, {}, relevant=True)[0])

    def test_unconfigured_place_and_no_target_are_never_paid_for(self):
        self.assertEqual(self.hold({"name": "Pune", "lat": "18.5204", "lon": "73.8567"}, {})[0],
                         "QUICKCOMMERCE_RESERVED_PLACE_NOT_CONFIGURED")
        self.assertEqual(self.hold(MUMBAI, {}, concept_ids=())[0], "QUICKCOMMERCE_RESERVED_CHECKLIST")

    def test_settings_default_to_the_saved_file(self):
        with patch("product_radar.quickcommerce_plan.admission_hold", return_value=None):
            self.assertIsNone(plan.paid_question_hold({}, ["sink"], MUMBAI, as_of=NOW))


class IssueRefreshTests(unittest.TestCase):
    def test_only_a_paid_row_of_a_type_no_longer_admitted_is_held(self):
        cache = {}
        self.assertFalse(plan.issue_refresh_held({}, "sink", "meta-ads", cache=cache))
        with patch("product_radar.quickcommerce_plan.admission_hold", side_effect=[None, HELD[1]]) as hold:
            self.assertFalse(plan.issue_refresh_held({}, "sink", "blinkit", cache=cache))
            self.assertFalse(plan.issue_refresh_held({}, "sink", "zepto", cache=cache))
            self.assertTrue(plan.issue_refresh_held({}, "other", "zepto", cache=cache))
        self.assertEqual(hold.call_count, 2)  # once per type


class NextQuestionTests(unittest.TestCase):
    def profile(self, main="draining sink caddy"):
        rows = [{"source": "quickcommerce", "text": "sink organizer", "scope": "family"},
                {"source": "quickcommerce", "text": main, "scope": "subtype"},
                {"source": "quickcommerce", "text": "sink area", "scope": "context"},
                {"source": "meta", "text": "sink rack", "scope": "subtype"}]
        return {"queries": rows, "aliases": ["sink holder"], "core_type": "sink organizer"}

    def ask(self, tasks=(), relevant=False, route="blinkit", as_of=NOW, profile=None):
        context = {"route": route}
        request = plan.next_question(context, profile or self.profile(), list(tasks), PLACES, run_id="week-one",
                                     as_of=as_of, relevant=lambda: relevant)
        return request, context["query_plan"]

    def task(self, place, status="acquired", run_id="week-one", at=NOW, query="draining sink caddy", **extra):
        task = dict({"request": {"query": query, "location": place}, "status": status, "run_id": run_id}, **extra)
        return task, at if status in plan.RESULT_STATES else None

    def test_one_main_phrase_in_mumbai_first(self):
        request, query_plan = self.ask()
        self.assertEqual(request, {"query": "draining sink caddy", "location": MUMBAI})
        self.assertEqual((query_plan["term_index"], query_plan["term_count"], query_plan["scope"]), (1, 1, "subtype"))
        self.assertEqual(query_plan["previous_task_ids"], [])

    def test_sparse_mumbai_result_never_tries_other_wording(self):
        for status in ("completed_no_results", "insufficient"):
            with self.subTest(status=status), self.assertRaisesRegex(SafetyError, "QUICKCOMMERCE_RESERVED_FIRST_PLACE"):
                self.ask([self.task(MUMBAI, status)])

    def test_other_places_follow_a_relevant_mumbai_result_in_order(self):
        request, query_plan = self.ask([self.task(MUMBAI)], relevant=True)
        self.assertEqual(request["location"], DELHI)
        self.assertIn("first place showed a relevant", query_plan["reason"])
        self.assertEqual(request["query"], "draining sink caddy")
        request, _ = self.ask([self.task(MUMBAI), self.task(DELHI, "completed_no_results")], relevant=True)
        self.assertEqual(request["location"], BENGALURU)
        done = [self.task(MUMBAI), self.task(DELHI), self.task(BENGALURU)]
        self.assertEqual(self.ask(done, relevant=True)[0]["location"], BENGALURU)  # re-joins, nothing new

    def test_an_unsettled_place_is_asked_again_instead_of_moving_on(self):
        for task in (self.task(MUMBAI, "prepared"), self.task(MUMBAI, "dispatching"),
                     self.task(MUMBAI, "blocked", reconcile_required=True),
                     self.task(MUMBAI, "failed", retryable=True, tries=1)):
            with self.subTest(task=task):
                self.assertEqual(self.ask([task], relevant=True)[0]["location"], MUMBAI)

    def test_a_failure_with_no_retry_left_is_settled(self):
        for task in (self.task(MUMBAI, "failed", retryable=False), self.task(MUMBAI, "blocked", retryable=True, tries=2),
                     self.task(MUMBAI, "failed", retryable=True, prelaunch_failures=2), self.task(MUMBAI, "waiting_input")):
            with self.subTest(task=task):
                self.assertEqual(self.ask([task], relevant=True)[0]["location"], DELHI)

    def test_a_place_answered_within_30_days_is_not_asked_again_in_another_run_or_wording(self):
        earlier = [self.task(MUMBAI, run_id="week-zero", query="sink organizer tray")]
        with self.assertRaisesRegex(SafetyError, "QUICKCOMMERCE_RESERVED_FIRST_PLACE"):
            self.ask(earlier, as_of=WEEK_LATER)
        self.assertEqual(self.ask(earlier, relevant=True, as_of=WEEK_LATER)[0]["location"], DELHI)
        everywhere = [self.task(place, run_id="week-zero") for place in (MUMBAI, DELHI, BENGALURU)]
        with self.assertRaisesRegex(SafetyError, "QUICKCOMMERCE_RESERVED_RECENT_CHECK"):
            self.ask(everywhere, relevant=True, as_of=WEEK_LATER)
        # After the recheck period the first place is asked again; failures never count as answers.
        self.assertEqual(self.ask(everywhere, as_of=MONTH_LATER)[0]["location"], MUMBAI)
        failed = [self.task(MUMBAI, "failed", run_id="week-zero", retryable=False)]
        self.assertEqual(self.ask(failed, as_of=WEEK_LATER)[0]["location"], MUMBAI)

    def test_answer_time_is_the_last_receipt_of_a_finished_question(self):
        state = {"source_checks": {"r1": {"at": NOW}, "r2": {"at": WEEK_LATER}}}
        self.assertEqual(plan.answered_at(state, {"status": "acquired", "receipt_refs": ["r1", "r2"]}), WEEK_LATER)
        for task in ({"status": "failed", "receipt_refs": ["r1"]}, {"status": "acquired", "receipt_refs": []},
                     {"status": "prepared"}):
            with self.subTest(task=task):
                self.assertIsNone(plan.answered_at(state, task))

    def test_no_reviewed_quickcommerce_phrase_is_a_named_gap(self):
        with self.assertRaisesRegex(SafetyError, "PRODUCT_QUERY_PROFILE_REQUIRED"):
            plan.next_question({"route": "zepto"}, {"queries": [{"source": "meta", "text": "x", "scope": "subtype"}]},
                               [], PLACES, run_id="week-one", as_of=NOW, relevant=lambda: True)


class BuilderTests(unittest.TestCase):
    """The installed planner: admission, reserved gaps and the place order."""

    setUp = coverage_fixtures.SourceCoverageTests.setUp
    product = coverage_fixtures.SourceCoverageTests.product
    query_profile = coverage_fixtures.SourceCoverageTests.query_profile
    query_context = coverage_fixtures.SourceCoverageTests.query_context
    receipt = coverage_fixtures.SourceCoverageTests.receipt
    direct_runner = coverage_fixtures.SourceCoverageTests.direct_runner
    run_sources = coverage_fixtures.SourceCoverageTests.run_sources

    def prepare(self, *, hold=None, relevant=False, run_id="week-one", as_of=NOW, profile=None):
        with patch("product_radar.quickcommerce_plan.admission_hold", return_value=hold), \
             patch("product_radar.quickcommerce_plan.first_place_relevant", return_value=relevant):
            if profile is None:
                return prepare_all_source_tasks(self.memory, run_id=run_id, concept_ids=["first"], as_of=as_of,
                                                request_builder=product_source_request_builder(run_id=run_id))
            with patch("product_radar.product_queries.resolve_query_profile", return_value=profile):
                return prepare_all_source_tasks(self.memory, run_id=run_id, concept_ids=["first"], as_of=as_of,
                                                request_builder=product_source_request_builder(run_id=run_id))

    def commerce_tasks(self):
        return {task["route"]: task for task in self.memory.snapshot()["query_tasks"].values() if task["route"] in APPS}

    def commerce_requests(self):
        return sorted((task["route"], task["request"]["location"]["name"])
                      for task in self.memory.snapshot()["query_tasks"].values() if task["route"] in APPS)

    def finish(self, state="empty"):
        def done(context):
            context["record_dispatch"]()
            return self.receipt(context, state=state)
        return self.run_sources({route: done for route in APPS})

    def test_held_back_types_are_reserved_for_later_not_failed(self):
        self.query_profile()
        for hold in HELD:
            with self.subTest(hold=hold):
                prepared = self.prepare(hold=hold)
                gaps = [row for row in prepared["pending_preparation"] if row["route"] in APPS]
                self.assertEqual({(row["route"], row["state"], row["reason"]) for row in gaps},
                                 {(route, "not_attempted", hold) for route in APPS})
                self.assertEqual(self.commerce_tasks(), {})
        installed = {route: (lambda context: None) for route in APPS}
        rows = [row for row in source_coverage(self.memory, handlers=installed, concept_ids=["first"], as_of=NOW)["rows"]
                if row["route"] in APPS]
        self.assertEqual({(row["state"], row["actual_attempt"]) for row in rows}, {("not_attempted", False)})
        self.assertTrue(all(row["reason"].startswith("QUICKCOMMERCE_RESERVED_") for row in rows))

    def test_admitted_type_asks_mumbai_on_all_three_apps_with_one_phrase(self):
        self.query_profile()
        self.prepare()
        tasks = self.commerce_tasks()
        self.assertEqual(set(tasks), set(APPS))
        for task in tasks.values():
            self.assertEqual(task["request"], {"query": "draining sink caddy", "location": MUMBAI})
            self.assertEqual(task["query_plans"]["first"]["term_count"], 1)
        # A sparse Mumbai answer neither broadens the wording nor opens other places.
        self.finish("empty")
        prepared = self.prepare(relevant=False)
        self.assertEqual(self.commerce_requests(), sorted((app, "Mumbai") for app in APPS))
        self.assertEqual({row["reason"] for row in prepared["pending_preparation"] if row["route"] in APPS},
                         {"QUICKCOMMERCE_RESERVED_FIRST_PLACE"})

    def test_relevant_mumbai_result_opens_delhi_then_bengaluru(self):
        self.query_profile()
        self.prepare(); self.finish("usable")
        self.prepare(relevant=True)
        self.assertEqual(self.commerce_requests(), sorted([(app, place) for app in APPS for place in ("Mumbai", "Delhi NCR")]))
        self.finish("empty")
        self.prepare(relevant=True)
        self.assertEqual(self.commerce_requests(),
                         sorted([(app, place) for app in APPS for place in ("Mumbai", "Delhi NCR", "Bengaluru")]))
        self.finish("empty")
        before = self.memory.snapshot()["query_tasks"]
        self.prepare(relevant=True)  # every place answered: nothing new is asked
        self.assertEqual(set(self.memory.snapshot()["query_tasks"]), set(before))

    def test_admission_is_checked_again_before_each_new_place(self):
        self.query_profile()
        self.prepare(); self.finish("usable")
        prepared = self.prepare(hold="QUICKCOMMERCE_RESERVED_CROWDING_HIGH", relevant=True)
        self.assertEqual(self.commerce_requests(), sorted((app, "Mumbai") for app in APPS))
        self.assertEqual({row["reason"] for row in prepared["pending_preparation"] if row["route"] in APPS},
                         {"QUICKCOMMERCE_RESERVED_CROWDING_HIGH"})

    def test_admission_is_worked_out_once_per_type_per_planning_pass(self):
        self.query_profile()
        with patch("product_radar.quickcommerce_plan.admission_hold", return_value=None) as hold, \
             patch("product_radar.research_flow.evidence_indexes", return_value=({}, {})) as indexes:
            prepare_all_source_tasks(self.memory, run_id="week-one", concept_ids=["first"], as_of=NOW,
                                     request_builder=product_source_request_builder(run_id="week-one"))
        self.assertEqual((hold.call_count, indexes.call_count), (1, 1))

    def test_a_new_week_does_not_repeat_places_answered_within_30_days(self):
        self.query_profile()
        self.prepare(); self.finish("empty")
        week = self.prepare(run_id="week-two", as_of=WEEK_LATER)
        self.assertEqual(self.commerce_requests(), sorted((app, "Mumbai") for app in APPS))
        self.assertEqual({row["reason"] for row in week["pending_preparation"] if row["route"] in APPS},
                         {"QUICKCOMMERCE_RESERVED_FIRST_PLACE"})
        self.prepare(run_id="week-six", as_of=MONTH_LATER)  # the month has passed: Mumbai is asked again
        self.assertEqual(self.commerce_requests(), sorted((app, "Mumbai") for app in APPS for _ in range(2)))

    def test_a_newly_reviewed_phrase_does_not_start_a_new_mumbai_triple(self):
        self.query_profile()
        self.prepare(); self.finish("empty")
        state = self.memory.snapshot()
        from product_radar.product_queries import resolve_query_profile
        changed = resolve_query_profile(state, "first")
        changed["queries"] = [dict(row, text="sink drainer rack") if row["source"] == "quickcommerce" else row
                              for row in changed["queries"]]
        prepared = self.prepare(profile=changed, as_of=WEEK_LATER)
        self.assertEqual(self.commerce_requests(), sorted((app, "Mumbai") for app in APPS))
        self.assertEqual({row["reason"] for row in prepared["pending_preparation"] if row["route"] in APPS},
                         {"QUICKCOMMERCE_RESERVED_FIRST_PLACE"})

    def test_bad_place_list_is_a_named_gap_not_a_guess(self):
        self.query_profile()
        with patch("product_radar.quickcommerce_plan.load_settings", side_effect=SafetyError("QUICKCOMMERCE_LOCATIONS_INVALID")):
            prepared = self.prepare()
        self.assertEqual({row["reason"] for row in prepared["pending_preparation"] if row["route"] in APPS},
                         {"QUICKCOMMERCE_LOCATIONS_INVALID"})
        self.assertEqual(self.commerce_tasks(), {})


class RemovedOptionTests(unittest.TestCase):
    def script(self, name):
        spec = importlib.util.spec_from_file_location(name.replace(".py", "_w5"), ROOT / "scripts" / name)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module

    def test_single_location_options_are_gone(self):
        programme, coverage = self.script("run_programme.py"), self.script("run_source_coverage.py")
        for main, argv in ((programme._main, ["--tick", "--execute", "--location-name", "Mumbai"]),
                           (programme._main, ["--programme-id", "p", "--latitude", "19.0760"]),
                           (coverage.main, ["--longitude", "72.8777"])):
            with self.subTest(argv=argv), redirect_stderr(StringIO()) as error, self.assertRaises(SystemExit):
                main(argv)
            self.assertIn("unrecognized arguments", error.getvalue())
        self.assertFalse((ROOT / "scripts/collect_quickcommerce.py").exists())
        with redirect_stderr(StringIO()) as error, self.assertRaises(SystemExit):
            coverage.main(["--execute", "--run-id", "r", "--funded-quickcommerce-fallback"])
        self.assertIn("unrecognized arguments", error.getvalue())

    def test_coverage_command_installs_no_paid_route(self):
        coverage = self.script("run_source_coverage.py")
        with tempfile.TemporaryDirectory() as folder:
            memory = ResearchMemory(Path(folder) / "r", Journal(Path(folder) / "f"), evidence_type="synthetic")
            memory.put("concepts", "sink", {"status": "active", "description": {"label": "Sink caddy"}})
            with patch.object(coverage, "source_coverage", wraps=coverage.source_coverage) as seen, \
                    patch("sys.stdout", new=StringIO()):
                self.assertEqual(coverage.main([], memory=memory), 0)
        installed = seen.call_args.kwargs["handlers"]
        self.assertFalse(set(APPS) & set(installed))

    def test_the_free_public_browser_route_is_gone(self):
        self.assertFalse((ROOT / "src/product_radar/quickcommerce_browser.py").exists())
        with self.assertRaises(ImportError):
            importlib.import_module("product_radar.quickcommerce_browser")

    def test_launchers_pass_no_location_flags(self):
        for path in ("scripts/windows/Install-Dropshipper.ps1", "scripts/windows/DropshipperDesktop.cs",
                     "scripts/setup_programme_task.ps1", "src/product_radar/programme_schedule.py",
                     "src/product_radar/desktop_control.py"):
            text = (ROOT / path).read_text(encoding="utf-8")
            for flag in ("--location-name", "--latitude", "--longitude"):
                self.assertNotIn(flag, text, path)


if __name__ == "__main__":
    unittest.main()
