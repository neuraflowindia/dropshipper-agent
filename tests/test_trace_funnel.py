"""Research trace funnel: every step in order, with the reasons items stopped.

The product-type judgements come from a made-up report pool shaped like the
programme's own (tests/trace_fixture.py); one test runs the programme's real
pool and meter functions on an empty synthetic store.
"""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import trace_fixture as fx
from product_radar import trace_funnel as funnel_module
from product_radar.models import SafetyError
from product_radar.trace_records import load_store


def by_key(step):
    return {count["key"]: count["value"] for count in step["counts"]}


def rows(step, title_start):
    table = next(table for table in step["tables"] if table["title"].startswith(title_start))
    return {row["code"]: row["value"] for row in table["rows"]}


def _store_with_task(store, values):
    import copy
    changed = copy.copy(store)
    changed.state = {**store.state, "query_tasks": {"task-x": {**fx.task("x", "x", "prepared", None, [fx.A]), **values}}}
    return changed


class TraceFunnelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name) / "store"
        fx.build(root)
        cls.store = load_store(root)
        cls.report = funnel_module.funnel(cls.store, fx.judged(), since=fx.START, as_of=fx.AS_OF,
                                          catalogue_loader=fx.catalogue, map_config=fx.MAP_CONFIG)
        cls.steps = {step["key"]: step for step in cls.report["steps"]}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_steps_are_in_pipeline_order(self):
        self.assertEqual([step["key"] for step in self.report["steps"]], [
            "headings", "discovery", "raw_results", "naming", "candidates", "definitions", "checklist", "suppliers",
            "photos", "market", "crowding", "scores", "labels", "codes", "eligible", "allocation"])
        self.assertEqual(self.report["meter"]["report_eligible_types"], 1)
        self.assertEqual(self.report["errors"], {})

    def test_headings(self):
        self.assertEqual(by_key(self.steps["headings"]), {"total": 4, "searched": 3, "searched_since": 3,
                                                          "unsearched": 1, "pace_per_day": 3.0})

    def test_headings_unreadable_map_is_named(self):
        def broken():
            raise SafetyError("CATALOGUE_INVALID")
        step = funnel_module.headings_step(self.store, fx.START, fx.AS_OF, catalogue_loader=broken, map_config={})
        self.assertIn("CATALOGUE_INVALID", step["notes"][0])
        self.assertEqual(step["counts"], [])

    def test_discovery(self):
        step = self.steps["discovery"]
        counts = by_key(step)
        self.assertEqual(counts["heading_searches"], 3)
        self.assertEqual(counts["wider_searches"], 1)
        self.assertEqual(counts["deodap"], "2 / 10")
        self.assertEqual(counts["dropdash"], "1 / not finished")
        self.assertEqual(rows(step, "Heading searches planned"), {"meta-ads": 2, "google-trends": 1})
        self.assertEqual(rows(step, "Heading searches on Meta"), {"interpretation_pending": 1, "not asked yet": 1})
        self.assertEqual(rows(step, "Heading searches on Google"), {"waiting: OWNER_SIGN_IN_WINDOW_OPEN": 1})
        self.assertEqual(rows(step, "Wider searches on tradeindia (supplier-listings)"), {"read": 1})
        self.assertIn("listings nominated: 3", step["tables"][3]["title"])
        self.assertEqual(rows(step, "Dropshipping-platform listings"), {"deodap": 1})
        questions = next(table for table in step["tables"] if table["title"].startswith("Research questions"))
        self.assertEqual([(row["code"], row["value"]) for row in questions["rows"]],
                         [("None | google-trends | prepared | SOURCE_ACCESS_PROBE_PENDING", 1)])
        self.assertIn("Google Trends [google-trends]: Not asked yet [prepared] — Waiting: one test read",
                      questions["rows"][0]["label"])
        self.assertEqual(funnel_module._question_label("sourcing | indiamart | acquired"),
                         "find suppliers for a type on IndiaMART [indiamart]: Answered with results [acquired]")

    def test_raw_results_and_naming(self):
        self.assertEqual(by_key(self.steps["raw_results"]), {"results": 3, "pages": 2})
        self.assertEqual(rows(self.steps["raw_results"], "Raw results by website"), {"meta": 1, "deodap": 1, "amazon": 1})
        self.assertEqual(by_key(self.steps["naming"]), {"listings": 2, "products": 1})
        self.assertEqual(rows(self.steps["naming"], "AI answer"), {"product": 1, "service": 1})
        self.assertEqual(rows(self.steps["naming"], "Listings whose naming"),
                         {"AI_RUNNER_NOT_ENABLED | open": 1, "AI_PLAN_LIMIT_CREDITS_PROTECTED | done_later": 1})

    def test_candidates_credit_listing_wave_or_unknown(self):
        step = self.steps["candidates"]
        self.assertEqual(by_key(step), {"created": 4})
        self.assertEqual(rows(step, "Created from"), {"deodap": 1, "amazon": 1,
                                                       "heading search on google-trends + meta-ads": 1, "not recorded": 1})
        self.assertEqual(rows(step, "What happened"), {"no_known_match": 1, "llm_same_type": 1, "llm_new_type": 1,
                                                        "out_of_scope": 1, "unclear": 1})
        self.assertEqual(rows(step, "Listings whose matching"),
                         {"AI_PLAN_LIMIT_CREDITS_PROTECTED | done_later": 1, "AI_RUNNER_NOT_ENABLED | open": 1,
                          "AI_RUNNER_NOT_ENABLED | done_later": 1})

    def test_definitions_and_checklist(self):
        definitions = by_key(self.steps["definitions"])
        self.assertEqual(definitions["active"], 5)
        self.assertEqual(definitions["definition_answers"], "1 / 0")
        self.assertEqual(rows(self.steps["definitions"], "Stopped at identity"), {"out_of_scope": 1})
        checklist = by_key(self.steps["checklist"])
        self.assertEqual((checklist["assessed"], checklist["passing"], checklist["below"]), (1, 1, 0))
        self.assertEqual(rows(self.steps["checklist"], "Number of Meets"), {7: 1})
        self.assertEqual(rows(self.steps["checklist"], "Types whose checklist"),
                         {"WORK_TURN_MODEL_LIMIT | done_later": 1, "WORK_TURN_MODEL_LIMIT | open": 1})
        self.assertEqual(funnel_module.problem_label("WORK_TURN_MODEL_LIMIT | open"),
                         "Waiting: this round's AI-use limit was used up [WORK_TURN_MODEL_LIMIT] — still waiting")

    def test_suppliers_and_photos(self):
        self.assertEqual(by_key(self.steps["suppliers"]), {"needed": 1, "with_leads": 1, "leads_found": 1,
                                                           "leads_checked": 2})
        self.assertEqual(rows(self.steps["suppliers"], "Supplier search status"),
                         {"complete": 1, "attempted": 1, "not recorded": 1})
        self.assertEqual(rows(self.steps["suppliers"], "Supplier questions to IndiaMART"),
                         {"failed: SUPPLIER_NATIVE_READ_FAILED": 1})
        self.assertEqual(rows(self.steps["suppliers"], "Supplier questions to TradeIndia"), {"held": 1})
        self.assertEqual(funnel_module._outcome_label("held: SOURCE_ACCESS_PROBE_PENDING"),
                         "Held back before anything was sent [blocked] — Waiting: one test read must succeed after a "
                         "refusal before others go [SOURCE_ACCESS_PROBE_PENDING]")
        self.assertEqual(rows(self.steps["suppliers"], "Supplier questions to Google Maps"), {"not asked yet": 1})
        self.assertEqual(by_key(self.steps["photos"]), {"downloaded": 1, "candidates": 1, "chosen": 1, "downloads": 2})

    def test_market_checks(self):
        step = self.steps["market"]
        self.assertEqual(by_key(step), {"demand": "5 / 3", "quickcommerce": "1 / 0", "dropship": "1 / 1"})
        self.assertEqual(rows(step, "Questions to Google Trends"), {"waiting: OWNER_SIGN_IN_WINDOW_OPEN": 1, "failed": 1})
        self.assertEqual(rows(step, "Questions to Meta"), {"completed_no_results": 1,
                                                            "held: SOURCE_ACCESS_PROBE_PENDING": 1})
        self.assertEqual(rows(step, "Questions to Blinkit"), {"waiting: QUICKCOMMERCE_RESERVED_CROWDING_NOT_CHECKED": 1})
        self.assertEqual(rows(step, "Answers from DeoDap"), {"interpretation_pending": 1})
        self.assertEqual(rows(step, "Demand checks (these count as market signals) — questions actually asked"),
                         {"amazon-search": 1, "meta-ads": 1, "google-trends": 1})
        self.assertEqual(rows(step, "Questions to Amazon search"), {"acquired": 1})
        self.assertEqual(rows(step, "Answers from Amazon search"), {"interpretation_pending": 1})
        self.assertEqual(rows(step, "Answers from Meta"), {"completed_no_results": 1})

    def test_crowding_scores_labels_codes(self):
        self.assertEqual((by_key(self.steps["crowding"])["high"], by_key(self.steps["crowding"])["not_checked"]), (1, 1))
        self.assertEqual(rows(self.steps["crowding"], "Why a type is not rated"), {"CROWDING_READS_PENDING": 1})
        scores = by_key(self.steps["scores"])
        self.assertEqual(scores["scored"], 2)
        self.assertEqual(scores["want"], "1 of 2 / 4.0")
        self.assertEqual(rows(self.steps["scores"], "Total score"), {16: 1, 52: 1})
        self.assertEqual(rows(self.steps["labels"], "Types per label"), {"sweet_spot": 1, "too_quiet": 1, "not scored": 2})
        self.assertEqual(by_key(self.steps["codes"]), {"types": 4, "several": 1})
        self.assertEqual(rows(self.steps["codes"], "Reason"), {"demand_too_quiet": 1, "crowding_pending": 1,
                                                               "supplier_search_required": 1, "out_of_scope": 1})
        self.assertTrue(self.steps["codes"]["tables"][0]["key"])

    def test_eligible_and_allocation(self):
        self.assertEqual(by_key(self.steps["eligible"]), {"eligible": 1, "meter": 1})
        self.assertEqual(self.steps["eligible"]["notes"], [f"Folding test rack ({fx.A})"])
        self.assertEqual(by_key(self.steps["allocation"]), {"allocated": 1, "reports_allocated": 1, "published": 1,
                                                            "reports": 0})
        self.assertEqual(rows(self.steps["allocation"], "Reports by allocation state"), {"reserved": 1})

    def test_per_day_uses_india_days(self):
        days = {row["day"]: row for row in self.report["per_day"]}
        self.assertEqual(sorted(days), ["2026-10-16", "2026-10-17"])
        first, second = days["2026-10-16"], days["2026-10-17"]
        self.assertEqual((first["raw_results"], first["product_types_created"], first["questions_created"]), (3, 4, 10))
        self.assertEqual((first["questions_answered"], first["website_reads_sent"], first["headings_first_searched"]),
                         (3, 3, 3))
        self.assertEqual((first["crowding_results"], first["photos_saved"], first["definitions_accepted"]), (2, 1, 1))
        self.assertEqual((first["ai_jobs_finished"], second["ai_jobs_finished"]), (1, 1))
        self.assertEqual((first["rounds_finished"], second["rounds_finished"]), (1, 1))

    def test_missing_pool_and_meter_still_give_every_step(self):
        report = funnel_module.funnel(self.store, {"pool": None, "meter": None, "errors": {"report_pool": "X"}},
                                      since=fx.START, as_of=fx.AS_OF, catalogue_loader=fx.catalogue,
                                      map_config=fx.MAP_CONFIG)
        steps = {step["key"]: step for step in report["steps"]}
        self.assertEqual(len(steps), 16)
        self.assertEqual(by_key(steps["eligible"])["eligible"], None)
        self.assertEqual(by_key(steps["scores"])["scored"], 0)
        self.assertEqual(report["errors"], {"report_pool": "X"})

    def test_route_groups_and_asked_rule(self):
        self.assertEqual(funnel_module.route_group({"route": "meta-ads-depth"}), "demand")
        self.assertEqual(funnel_module.route_group({"route": "zepto"}), "quickcommerce")
        self.assertEqual(funnel_module.route_group({"route": "dropdash"}), "dropship")
        self.assertEqual(funnel_module.route_group({"route": "google-suppliers"}), "supplier")
        self.assertEqual(funnel_module.route_group({"route": "new-site", "source": "x"}), "other")
        self.assertFalse(funnel_module.was_asked({"status": "blocked", "attempt_refs": []}))
        self.assertTrue(funnel_module.was_asked({"status": "blocked", "attempt_refs": ["a"]}))
        self.assertTrue(funnel_module.was_asked({"status": "dispatching", "dispatch_started": True}))
        other = funnel_module.market_step(_store_with_task(self.store, {"route": "new-site", "source": "x"}), None)
        self.assertEqual(by_key(other)["other"], "1 / 0")

    def test_unreadable_definitions_are_named(self):
        with patch("product_radar.functional_types.functional_type_inventory", side_effect=KeyError("x")):
            step = funnel_module.definitions_step(self.store, fx.judged())
        self.assertIn("KeyError", step["notes"][0])

    def test_crowding_unreadable_is_named(self):
        with patch("product_radar.crowding.crowding_summary", side_effect=SafetyError("CROWDING_BAD")):
            step = funnel_module.crowding_step(self.store)
        self.assertIn("CROWDING_BAD", step["notes"][0])

    def test_judge_uses_the_given_programme_functions_and_names_failures(self):
        seen = {}

        def builder(source, **options):
            seen["state"] = source.snapshot()
            seen["options"] = options
            return {"decisions": [], "eligible": []}

        result = funnel_module.judge(self.store, fx.AS_OF, pool_builder=builder,
                                     counter=lambda *args: {"report_eligible_types": 0})
        self.assertEqual(result["errors"], {})
        self.assertEqual(seen["options"]["as_of"], fx.AS_OF)
        self.assertIsNot(seen["state"], self.store.state)  # the programme reads its own copy
        broken = funnel_module.judge(self.store, fx.AS_OF, pool_builder=lambda *a, **k: (_ for _ in ()).throw(TypeError()),
                                     counter=lambda *args: (_ for _ in ()).throw(SafetyError("METER_BAD")))
        self.assertEqual(broken["errors"], {"report_pool": "TypeError", "meter_counts": "METER_BAD"})
        self.assertIsNone(broken["pool"])

    def test_judge_with_the_programmes_own_functions_on_an_empty_store(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fx.write_events(root / "data" / "research" / "captured" / "events", [])
            result = funnel_module.judge(load_store(root), fx.AS_OF)
        self.assertEqual(result["errors"], {})
        self.assertEqual(result["pool"]["decisions"], [])
        self.assertEqual(result["meter"]["candidates"], 0)
        self.assertEqual(result["meter"]["report_eligible_types"], 0)


if __name__ == "__main__":
    unittest.main()
