"""Local planner behavior, not a prediction of search yield or source access."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.catalogue import CATEGORIES, alias_groups, catalogue_summary, load_catalogue, validate_catalogue
from product_radar.models import SafetyError, digest
from product_radar.research_planner import DEFAULT_ROUTES, cooldown_until, plan_searches, semantic_key, validate_outcome
from product_radar.storage import atomic_json

NOW = "2026-10-06T10:00:00+00:00"


def catalogue(size=3):
    result = {"schema_version": 1, "revision": "fixture-v1", "built_at": NOW,
              "sources": [{"id": "page1", "url": "https://example.org/category", "title": "Synthetic category fixture",
                           "checked_at": NOW, "excerpts": ["Fixture only; not real source research."]}], "branches": [], "families": []}
    for category_index, category in enumerate(CATEGORIES):
        parent = "branch" + str(category_index)
        result["branches"].append({"id": parent, "parent_id": None, "category": category, "name": category})
        for n in range(size):
            family_id = f"f{category_index}-{n}"
            label = f"Distinct fixture product {category_index} {n}"
            result["families"].append({"id": family_id, "parent_id": parent, "category": category,
                                      "label": label, "definition": "A distinguishable fixture function.", "problem": "Fixture problem.",
                                      "included": [label], "excluded": ["Different mechanism."], "aliases": [], "overlaps": [],
                                      "examples": [{"label": label, "basis": "observed", "sources": ["page1"]}],
                                      "basis": "observed", "sources": ["page1"], "rationale": "Fixture breadth.",
                                      "phrases": [{"text": label, "source": "meta", "purpose": "discovery", "language": "en",
                                                   "country": "IN", "reason": "A narrow fixture term."}],
                                      "season": {"kind": "evergreen", "months": []}, "review_note": "No live market judgment.",
                                      "disposition": "usable", "author": "test", "reviewed_at": NOW, "next_review": "After measured results."})
    return result


def outcome(action, n=1, days=1, **changes):
    value = {"id": f"capture{n}", "acquisition_id": f"acquisition{n}", "family_id": action["family_id"],
             "source": action["source"], "status": "completed", "request": deepcopy(action["request"]),
             "observed_at": (datetime.fromisoformat(NOW) - timedelta(days=days)).isoformat(),
             "complete": True, "reviewed": True, "resolved_count": 10, "known_count": 8,
             "pending_count": 0, "unseen_promising": False, "useful_progress": False}
    value.update(changes)
    return value


class CatalogueTests(unittest.TestCase):
    def test_load_and_counts_do_not_count_phrase_variants_as_distinct_families(self):
        data = catalogue()
        data["families"][0]["phrases"].append(dict(data["families"][0]["phrases"][0], text="Alternative coverage", reason="Different useful coverage."))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "catalogue.json"
            atomic_json(path, data)
            self.assertEqual(load_catalogue(path), data)
        summary = catalogue_summary(data)
        self.assertEqual(summary["distinct_usable_families"], 12)
        self.assertEqual(summary["phrase_variants"], 13)
        self.assertEqual(summary["rough_weeks_at_assumed_rate"], 1.5)

    def test_same_wording_for_two_sources_is_two_options_not_two_phrase_variants(self):
        data = catalogue()
        phrase = data["families"][0]["phrases"][0]
        data["families"][0]["phrases"].append(dict(phrase, source="trends", purpose="validation"))
        summary = catalogue_summary(data)
        self.assertEqual(summary["phrase_variants"], 12)
        self.assertEqual(summary["source_purpose_options"], 13)

    def test_branch_cycle_is_invalid(self):
        data = catalogue()
        data["branches"][0]["parent_id"] = data["branches"][0]["id"]
        with self.assertRaisesRegex(SafetyError, "CYCLE"):
            validate_catalogue(data)

    def test_parent_category_must_align_with_service(self):
        data = catalogue()
        data["families"][0]["parent_id"] = "branch1"
        with self.assertRaisesRegex(SafetyError, "PARENT"):
            validate_catalogue(data)

    def test_observed_examples_require_real_references_but_plausible_ideas_do_not(self):
        data = catalogue()
        family = data["families"][0]
        family["sources"] = []
        with self.assertRaisesRegex(SafetyError, "SOURCE_REFERENCE"):
            validate_catalogue(data)
        family["basis"] = "plausible_extension"
        family["examples"][0].update(basis="plausible_extension", sources=[])
        self.assertEqual(validate_catalogue(data), data)

    def test_unknown_citation_cannot_be_declared_observed(self):
        data = catalogue()
        data["families"][0]["examples"][0]["sources"] = ["missing"]
        with self.assertRaisesRegex(SafetyError, "EXAMPLE"):
            validate_catalogue(data)

    def test_exact_aliases_share_inventory_count_not_semantic_overlap_links(self):
        data = catalogue()
        data["families"][1]["aliases"] = [data["families"][0]["label"].upper()]
        data["families"][2]["overlaps"] = [data["families"][0]["id"]]
        groups = alias_groups(data)
        self.assertEqual(groups["f0-0"], groups["f0-1"])
        self.assertNotEqual(groups["f0-0"], groups["f0-2"])
        self.assertEqual(catalogue_summary(data)["distinct_usable_families"], 11)

    def test_bad_month_bool_is_not_a_month(self):
        data = catalogue()
        data["families"][0]["season"]["months"] = [True]
        with self.assertRaisesRegex(SafetyError, "SEASON"):
            validate_catalogue(data)

    def test_public_source_cannot_embed_account_credentials(self):
        for url in ("https://person:password@example.org/", "https://example.org/?access_token=private"):
            data = catalogue()
            data["sources"][0]["url"] = url
            with self.assertRaisesRegex(SafetyError, "SOURCE"):
                validate_catalogue(data)


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.data = catalogue()

    def plan(self, outcomes=None, **options):
        return plan_searches(self.data, outcomes or [], as_of=NOW, **options)

    def first(self):
        return self.plan(slots=1)["actions"][0]

    def test_frozen_inputs_are_repeatable_without_mutation(self):
        before = digest(self.data)
        first = self.plan()
        self.assertEqual(first, self.plan())
        self.assertEqual(before, digest(self.data))
        self.assertEqual(len(first["actions"]), 8)
        self.assertEqual(len(first["decisions"]), 12)

    def test_cold_start_rotates_categories_instead_of_four_phrases_in_one_category(self):
        result = self.plan(slots=4)
        self.assertEqual(len({item["category"] for item in result["actions"]}), 4)

    def test_single_slot_does_not_force_four_requests(self):
        self.assertEqual(len(self.plan(slots=1)["actions"]), 1)
        self.assertEqual(self.plan(slots=0)["actions"], [])

    def test_mixed_urgent_new_and_cached_families_have_comparable_ranks(self):
        first = self.first()
        history = [outcome(first, useful_progress=True)]
        before = digest({"catalogue": self.data, "history": history})
        options = {"slots": 2, "urgent_family_ids": [first["family_id"], "f1-0"]}
        result = self.plan(history, **options)
        self.assertEqual([a["family_id"] for a in result["actions"]], ["f1-0", first["family_id"]])
        self.assertEqual([a["kind"] for a in result["actions"]], ["collect", "reuse"])
        self.assertEqual(result, self.plan(history, **options))
        self.assertEqual(before, digest({"catalogue": self.data, "history": history}))

    def test_mixed_urgent_pool_keeps_fourth_slot_for_a_useful_revisit(self):
        self.data = catalogue(size=5)
        first = self.first()
        history = [outcome(first, days=8, useful_progress=True)]
        result = self.plan(history, slots=4, urgent_family_ids=[first["family_id"], "f1-0", "f2-0", "f3-0", "f0-1"])
        self.assertTrue(all(a["family_id"] != first["family_id"] for a in result["actions"][:3]))
        self.assertEqual(result["actions"][3]["family_id"], first["family_id"])

    def test_long_history_still_chooses_unexplored_areas_with_bounded_actions(self):
        action = self.first()
        for count in (30, 50, 80):
            history = [outcome(action, n=n, days=n + 15, useful_progress=True) for n in range(1, count + 1)]
            result = self.plan(history, slots=4)
            self.assertEqual(len(result["actions"]), 4)
            self.assertNotEqual(result["actions"][0]["family_id"], action["family_id"])
            self.assertEqual(result, self.plan(list(reversed(history)), slots=4))

    def test_aliases_and_phrase_variants_do_not_make_new_families(self):
        self.data["families"][1]["aliases"] = [self.data["families"][0]["label"]]
        self.data["families"][0]["phrases"].append(dict(self.data["families"][0]["phrases"][0], text="Spelling alternative"))
        result = self.plan(slots=12)
        self.assertEqual(len(result["actions"]), 11)
        self.assertEqual(sum(item["family_id"] == "f0-0" for item in result["actions"]), 1)
        history = [outcome(result["actions"][0], status="pending", reviewed=False)]
        pending = self.plan(history, slots=12)
        self.assertTrue(all(item["family_id"] not in ("f0-0", "f0-1") for item in pending["actions"]))

    def test_history_reorders_recently_explored_category(self):
        action = self.first()
        result = self.plan([outcome(action, days=8)], slots=1)
        self.assertNotEqual(result["actions"][0]["category"], action["category"])

    def test_relevant_season_preferred_without_excluding_the_whole_category(self):
        self.data["families"][0]["season"] = {"kind": "seasonal", "months": [1]}
        result = self.plan(slots=1)
        self.assertEqual(result["actions"][0]["family_id"], "f0-1")
        self.assertTrue(any(item["family_id"] == "f0-0" for item in self.plan(slots=12)["actions"]))

    def test_purposeful_revisit_can_take_fourth_slot_after_three_unexplored(self):
        action = self.first()
        result = self.plan([outcome(action, days=8, useful_progress=True)], slots=4)
        self.assertEqual(result["actions"][3]["family_id"], action["family_id"])
        self.assertEqual(result["actions"][3]["kind"], "collect")

    def test_four_fresh_captures_do_not_starve_unexplored_discovery_slots(self):
        visited = self.plan(slots=4)["actions"]
        history = [outcome(action, n=n + 1, unseen_promising=True, useful_progress=True)
                   for n, action in enumerate(visited)]
        # Mirrors three interpreted captures plus one downloaded capture whose
        # descriptions still need finishing. Neither status makes a new area.
        history[-1].update(reviewed=False, pending_count=20, resolved_count=0,
                           known_count=0, unseen_promising=None, useful_progress=None)
        visited_ids = {action["family_id"] for action in visited}
        for slots in (1, 2, 3, 4):
            with self.subTest(slots=slots):
                result = self.plan(history, slots=slots)
                self.assertEqual(len(result["actions"]), slots)
                first_new = result["actions"][:min(slots, 3)]
                self.assertTrue(all(action["kind"] == "collect" and action["family_id"] not in visited_ids
                                    for action in first_new))
                if slots == 4:
                    self.assertEqual(result["actions"][3]["kind"], "reuse")
                    self.assertIn(result["actions"][3]["family_id"], visited_ids)

    def test_reuse_counts_as_a_discovery_slot_not_another_revisit_turn(self):
        self.data = catalogue(size=8)
        visited = self.plan(slots=4)["actions"]
        history = [outcome(action, n=n + 1, useful_progress=True) for n, action in enumerate(visited)]
        visited_ids = {action["family_id"] for action in visited}
        result = self.plan(history, slots=8)
        self.assertEqual([action["kind"] for action in result["actions"]],
                         ["collect", "collect", "collect", "reuse", "collect", "collect", "collect", "reuse"])
        self.assertEqual(sum(action["family_id"] not in visited_ids for action in result["actions"]), 6)

    def test_thirty_fifty_eighty_fresh_history_rows_cannot_fill_every_new_slot(self):
        self.data = catalogue(size=8)
        visited = self.plan(slots=4)["actions"]
        visited_ids = {action["family_id"] for action in visited}
        for count in (30, 50, 80):
            with self.subTest(count=count):
                history = [outcome(visited[n % len(visited)], n=n + 1, useful_progress=True)
                           for n in range(count)]
                result = self.plan(history, slots=8)
                self.assertEqual(sum(action["family_id"] not in visited_ids for action in result["actions"]), 6)
                self.assertEqual(result, self.plan(list(reversed(history)), slots=8))

    def test_no_affordable_new_work_still_allows_saved_capture_reuse(self):
        visited = self.plan(slots=4)["actions"]
        history = [outcome(action, n=n + 1, useful_progress=True) for n, action in enumerate(visited)]
        result = self.plan(history, slots=4, budget_usd="0.06", validation_reserve_usd="0.06")
        self.assertEqual(len(result["actions"]), 4)
        self.assertTrue(all(action["kind"] == "reuse" for action in result["actions"]))
        self.assertEqual(result["budget"]["discovery_estimate_usd"], "0")
        self.assertTrue(any(item["decision"] == "defer" and "allowance" in item["reason"]
                            for item in result["decisions"]))

    def test_explicit_urgent_saved_capture_can_precede_new_work_without_starvation(self):
        visited = self.plan(slots=4)["actions"]
        history = [outcome(action, n=n + 1, useful_progress=True) for n, action in enumerate(visited)]
        result = self.plan(history, slots=4, urgent_family_ids=[visited[0]["family_id"]])
        self.assertEqual(result["actions"][0]["family_id"], visited[0]["family_id"])
        self.assertEqual(result["actions"][0]["kind"], "reuse")
        self.assertIn("relevant-lead", result["actions"][0]["reason"])
        self.assertTrue(all(action["kind"] == "collect" for action in result["actions"][1:3]))

    def test_semantic_cache_key_ignores_run_tags_but_keeps_build_locale_scope(self):
        request = self.first()["request"]
        copy = dict(request, attempt_id="another-run", run_tag="new-week")
        self.assertEqual(semantic_key(copy), semantic_key(request))
        for field, value in (("build", "next-version"), ("language", "hi-Latn"), ("coverage", {"limit": 21}),
                             ("date_scope", {"from": "2026-09-01", "to": "2026-10-01"}), ("options", {"active": True})):
            changed = dict(request, **{field: value})
            self.assertNotEqual(semantic_key(changed), semantic_key(request))

    def test_equivalent_fresh_complete_capture_reused_not_relabelled_as_collected_today(self):
        action = self.first()
        previous = outcome(action)
        result = self.plan([previous], slots=12)
        reuse = next(item for item in result["actions"] if item["family_id"] == action["family_id"])
        self.assertEqual(reuse["kind"], "reuse")
        self.assertEqual(reuse["capture_id"], previous["id"])
        self.assertEqual(reuse["captured_observed_at"], previous["observed_at"])
        self.assertEqual(previous["observed_at"], outcome(action)["observed_at"])
        self.assertEqual(result["actions"][0]["kind"], "collect", "Explore a distinct unvisited family before the saved-work revisit slot")

    def test_capped_capture_only_covers_equal_or_smaller_request(self):
        action = self.first()
        previous = outcome(action)
        previous["request"]["coverage"]["limit"] = 10
        result = self.plan([previous], slots=12)
        self.assertEqual(next(item for item in result["actions"] if item["family_id"] == action["family_id"])["kind"], "collect")
        previous["request"]["coverage"]["limit"] = 30
        result = self.plan([previous], slots=12)
        self.assertEqual(next(item for item in result["actions"] if item["family_id"] == action["family_id"])["kind"], "reuse")

    def test_failed_partial_future_and_stale_captures_do_not_hit_cache(self):
        action = self.first()
        for changes in ({"status": "failed"}, {"status": "partial"}, {"complete": False}, {"days": -1}, {"days": 8}):
            days = changes.get("days", 1)
            previous = outcome(action, days=days, **{k: v for k, v in changes.items() if k != "days"})
            result = self.plan([previous], slots=12)
            match = next(item for item in result["actions"] if item["family_id"] == action["family_id"])
            self.assertEqual(match["kind"], "collect")

    def test_two_reviewed_comparable_repeat_samples_trigger_temporary_cooldown(self):
        action = self.first()
        samples = [outcome(action, n=1, days=9), outcome(action, n=2, days=8)]
        self.assertIsNotNone(cooldown_until(samples, as_of=NOW))
        result = self.plan(samples, slots=12)
        self.assertTrue(all(item["family_id"] != action["family_id"] for item in result["actions"]))
        decision = next(item for item in result["decisions"] if item["family_id"] == action["family_id"])
        self.assertIn("next_eligible_at", decision)

    def test_cooldown_expires_at_actual_calendar_boundary_not_number_of_batches(self):
        action = self.first()
        samples = [outcome(action, n=1, days=15), outcome(action, n=2, days=14)]
        self.assertIsNone(cooldown_until(samples, as_of=NOW))
        samples[1]["observed_at"] = (datetime.fromisoformat(NOW) - timedelta(days=13, hours=23)).isoformat()
        self.assertIsNotNone(cooldown_until(samples, as_of=NOW))

    def test_errors_pending_identity_small_samples_and_unknown_progress_are_not_saturation(self):
        action = self.first()
        for changes in ({"status": "failed"}, {"status": "partial"}, {"reviewed": False}, {"pending_count": 80},
                        {"resolved_count": 9, "known_count": 9}, {"useful_progress": None}, {"known_count": 7}):
            samples = [outcome(action, n=1, days=9), outcome(action, n=2, days=8, **changes)]
            self.assertIsNone(cooldown_until(samples, as_of=NOW), changes)

    def test_one_sample_and_same_capture_imported_twice_do_not_saturate(self):
        action = self.first()
        sample = outcome(action, days=8)
        self.assertIsNone(cooldown_until([sample], as_of=NOW))
        self.assertIsNone(cooldown_until([sample, dict(sample, id="anotherreview")], as_of=NOW))

    def test_different_build_or_scope_samples_are_not_comparable(self):
        action = self.first()
        first, second = outcome(action, n=1, days=9), outcome(action, n=2, days=8)
        for field, value in (("build", "another-build"), ("term", "another meaningful term"), ("coverage", {"limit": 10})):
            second["request"] = dict(first["request"], **{field: value})
            self.assertIsNone(cooldown_until([first, second], as_of=NOW))

    def test_a_new_useful_lead_reopens_a_cooled_family(self):
        action = self.first()
        samples = [outcome(action, n=1, days=9), outcome(action, n=2, days=8),
                   outcome(action, n=3, days=7, unseen_promising=True)]
        self.assertIsNone(cooldown_until(samples, as_of=NOW))

    def test_pending_acquisition_is_resumed_not_replaced(self):
        action = self.first()
        result = self.plan([outcome(action, status="pending", reviewed=False)], slots=12)
        self.assertTrue(all(item["family_id"] != action["family_id"] for item in result["actions"]))
        self.assertIn("resume", next(item for item in result["decisions"] if item["family_id"] == action["family_id"])["reason"])

    def test_budget_reserves_validation_money_and_does_not_force_slots(self):
        result = self.plan(slots=8, budget_usd="0.15", per_search_usd="0.03", validation_reserve_usd="0.06")
        self.assertEqual(len(result["actions"]), 3)
        self.assertEqual(result["budget"]["discovery_estimate_usd"], "0.09")
        self.assertTrue(any("allowance" in item["reason"] for item in result["decisions"]))

    def test_unknown_route_cost_is_not_free_under_a_budget(self):
        routes = deepcopy(DEFAULT_ROUTES)
        routes["meta"]["cost_estimate_usd"] = None
        result = self.plan(slots=8, budget_usd="1", routes=routes)
        self.assertEqual(result["actions"], [])
        self.assertTrue(all("unknown cost" in item["reason"] for item in result["decisions"]))
        unconstrained = self.plan(slots=1, routes=routes)
        self.assertIsNone(unconstrained["actions"][0]["cost_estimate_usd"])
        self.assertTrue(unconstrained["budget"]["has_unknown_cost"])

    def test_explicit_verified_zero_cost_route_is_allowed_without_infinite_yield_priority(self):
        routes = deepcopy(DEFAULT_ROUTES)
        routes["meta"].update(cost_estimate_usd="0", estimate_basis="confirmed local route")
        result = self.plan(slots=4, budget_usd="0.06", routes=routes)
        self.assertEqual(len(result["actions"]), 4)
        self.assertTrue(all(item["cost_estimate_usd"] == "0" for item in result["actions"]))

    def test_unavailable_route_does_not_block_reuse_or_an_independent_available_category(self):
        action = self.first()
        routes = deepcopy(DEFAULT_ROUTES)
        routes["meta"]["available"] = False
        result = self.plan([outcome(action)], slots=4, routes=routes)
        self.assertEqual(len(result["actions"]), 1)
        self.assertEqual(result["actions"][0]["kind"], "reuse")
        self.assertTrue(any(item.get("route_details") for item in result["decisions"]))

    def test_urgent_relevant_lead_override_has_a_recorded_reason(self):
        action = self.first()
        samples = [outcome(action, n=1, days=9), outcome(action, n=2, days=8)]
        result = self.plan(samples, slots=1, urgent_family_ids=[action["family_id"]])
        self.assertEqual(result["actions"][0]["family_id"], action["family_id"])
        self.assertIn("relevant-lead", result["actions"][0]["reason"])

    def test_outcome_invalid_counts_and_binary_float_costs_are_rejected(self):
        action = self.first()
        for changes in ({"resolved_count": True}, {"known_count": 11}, {"new_eligible_count": -1}, {"cost_usd": 0.03}):
            with self.assertRaises(SafetyError):
                validate_outcome(outcome(action, **changes))

    def test_timezone_offsets_do_not_reverse_actual_last_visit_order(self):
        initial = self.plan(slots=4)["actions"]
        history = []
        for n, action in enumerate(initial):
            observation = outcome(action, n=n + 1, days=8)
            instant = datetime(2026, 9, 20, 10 + n, tzinfo=timezone.utc)
            observation["observed_at"] = instant.astimezone(timezone(timedelta(hours=12 if n == 0 else -8))).isoformat()
            history.append(observation)
        result = self.plan(history, slots=1)
        self.assertEqual(result["actions"][0]["category"], initial[0]["category"])


if __name__ == "__main__":
    unittest.main()
