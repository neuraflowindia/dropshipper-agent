"""Heading order for discovery waves (Plan R10 W10): never-served headings
first, then headings with fresh signals, then the one served longest ago;
headings whose types all came out High crowding last; categories take turns;
sub-areas spread; no alphabetical order. Synthetic records only."""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from product_radar import heading_order as order
from product_radar.catalogue_discovery import VERSION, WAVE_VERSION
from product_radar.models import SafetyError
from product_radar.research_memory import key
from product_radar.storage import atomic_json
from product_radar.service import CATEGORIES

AT = "2026-10-15T06:00:00+00:00"


def family(family_id, label, category="Home and Kitchen", parent="branch-a", phrases=()):
    return {"id": family_id, "label": label, "aliases": [], "category": category, "parent_id": parent,
            "phrases": [{"text": text} for text in phrases]}


def state(**tables):
    value = {name: {} for name in ("batch_runs", "query_outcomes", "observations", "associations", "concepts")}
    value.update(tables)
    return value


def wave(wave_id, prepared_at, rows, kind=WAVE_VERSION):
    """rows: (family_id, question_id, acquisition_id or None)."""
    return {"kind": kind, "id": wave_id, "prepared_at": prepared_at,
            "planned_actions": [{"family_id": fid, "questions": [{"id": qid}]} for fid, qid, _ in rows],
            "actions": {qid: {"acquisition_id": aid} for _, qid, aid in rows if aid}}


class ServedAndCrowdedTests(unittest.TestCase):
    def test_last_serving_comes_from_waves_cycles_and_old_query_outcomes(self):
        found, owners = order.served_times(state(batch_runs={
            "w1": wave("w1", "2026-10-10T00:00:00+00:00", [("a", "q1", "acq-1"), ("b", "q2", None)]),
            "w2": wave("w2", "2026-10-12T00:00:00+00:00", [("a", "q3", "acq-3")]),
            "c1": wave("c1", "2026-10-11T00:00:00+00:00", [("c", "q4", "acq-4")], kind=VERSION),
            "other": {"kind": "work-turn-v1"}},
            query_outcomes={"o": {"family_id": "d", "observed_at": "2026-10-09T00:00:00+00:00"},
                            "o2": {"family_id": "c", "observed_at": "2026-10-13T00:00:00+00:00"}}))
        self.assertEqual(found, {"a": "2026-10-12T00:00:00+00:00", "b": "2026-10-10T00:00:00+00:00",
                                 "c": "2026-10-13T00:00:00+00:00", "d": "2026-10-09T00:00:00+00:00"})
        self.assertEqual(owners, {"acq-1": "a", "acq-3": "a", "acq-4": "c"})

    def test_only_headings_whose_known_types_are_all_high_are_crowded(self):
        records = state(
            observations={"o1": {"acquisition_id": "acq-a", "semantic_id": "s1"},
                          "o2": {"acquisition_id": "acq-b", "semantic_id": "s2"},
                          "o3": {"acquisition_id": "acq-b", "semantic_id": "s3"},
                          "o4": {"acquisition_id": "acq-c", "semantic_id": "s4"},
                          "o5": {"acquisition_id": "elsewhere", "semantic_id": "s5"}},
            associations={"x1": {"status": "resolved", "semantic_id": "s1", "concept_id": "high-1"},
                          "x2": {"status": "resolved", "semantic_id": "s2", "concept_id": "high-2"},
                          "x3": {"status": "resolved", "semantic_id": "s3", "concept_id": "low-1"},
                          "x4": {"status": "resolved", "semantic_id": "s4", "concept_id": "unknown-1"},
                          "x5": {"status": "excluded", "semantic_id": "s4", "concept_id": None},
                          "x6": {"status": "resolved", "semantic_id": "s5", "concept_id": "high-3"}},
            concepts={cid: {} for cid in ("high-1", "high-2", "low-1", "unknown-1", "high-3")},
            # Saved facts: a heading without a saved High type needs no full read.
            batch_runs={key("crowding-fact", cid): {"level": "high"} for cid in ("high-1", "high-2", "high-3")})
        levels = {"high-1": "high", "high-2": "high", "low-1": "low", "unknown-1": "not_checked", "high-3": "high"}
        owners = {"acq-a": "a", "acq-b": "b", "acq-c": "c"}
        with patch("product_radar.crowding.crowding_for", side_effect=lambda _s, cid, **_o: {"level": levels[cid]}) as read:
            self.assertEqual(order.crowded_headings(records, owners, thresholds={"version": "t"}), {"a"})
        self.assertEqual(sorted(call.args[1] for call in read.call_args_list), ["high-1", "high-2", "low-1"])
        # One type's unreadable fact never moves the heading; a damaged store is raised.
        with patch("product_radar.crowding.crowding_for", side_effect=SafetyError("FUNCTIONAL_DEFINITION_INVALID")):
            self.assertEqual(order.crowded_headings(records, owners, thresholds={"version": "t"}), set())
        with patch("product_radar.crowding.crowding_for", side_effect=SafetyError("RESEARCH_EVENTS_INVALID")):
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
                order.crowded_headings(records, owners, thresholds={"version": "t"})
        self.assertEqual(order.crowded_headings(state(), {}), set())


class FreshAndKeyTests(unittest.TestCase):
    def setUp(self):
        self.catalogue = {"families": [
            family("chopper", "Vegetable chopper", phrases=["onion chopper"]),
            family("rack", "Spice rack", category="Home and Decor", parent="branch-b"),
            family("bottle", "Baby feeding bottle", category="Baby Products", parent="branch-c"),
            family("tent", "Pop up tent", category="Travel and Outdoor", parent="branch-d")]}

    def test_fresh_signals_count_only_after_the_last_serving_and_by_whole_heading_words(self):
        from product_radar.map_materials import word_set
        signals = [(word_set("Electric onion chopper with bowl"), "2026-10-14T00:00:00+00:00"),
                   (word_set("Vegetable chopper 3 blades"), "2026-10-09T00:00:00+00:00"),
                   (word_set("Rotating spice rack"), "2026-10-14T00:00:00+00:00"),
                   (word_set("Spice jar"), "2026-10-14T00:00:00+00:00")]
        served = {"chopper": "2026-10-10T00:00:00+00:00", "rack": "2026-10-14T12:00:00+00:00"}
        self.assertEqual(order.fresh_counts(self.catalogue, signals, served), {"chopper": 1})
        self.assertEqual(order.fresh_counts(self.catalogue, signals, {}), {"chopper": 2, "rack": 1})

    def ordered(self, families, facts, categories=None):
        facts["turns"] = order.heading_turns({"families": families}, facts)
        return sorted(families, key=lambda row: order.heading_key(row, facts, categories or list(CATEGORIES)))

    def test_never_served_then_fresh_then_oldest_served_and_crowded_last(self):
        facts = {"served": {"chopper": "2026-10-12T00:00:00+00:00", "rack": "2026-10-10T00:00:00+00:00",
                            "tent": "2026-10-13T00:00:00+00:00"},
                 "fresh": {"tent": 2, "bottle": 5}, "crowded": set(), "branch_served": {}}
        rows = self.ordered(self.catalogue["families"], facts)
        self.assertEqual([row["id"] for row in rows], ["bottle", "tent", "rack", "chopper"])
        facts["crowded"] = {"bottle", "tent"}
        facts["turns"] = order.heading_turns(self.catalogue, facts)
        rows = sorted(self.catalogue["families"], key=lambda row: order.heading_key(row, facts, list(CATEGORIES)))
        self.assertEqual([row["id"] for row in rows], ["rack", "chopper", "bottle", "tent"])

    def test_categories_take_turns_then_thin_sub_areas_then_a_spread_not_the_alphabet(self):
        facts = {"served": {}, "fresh": {}, "crowded": set(), "branch_served": {}}
        rotated = list(CATEGORIES)[2:] + list(CATEGORIES)[:2]
        rows = self.ordered(self.catalogue["families"], facts, rotated)
        self.assertEqual([row["category"] for row in rows], rotated)
        # Two headings per category: the first of each category, then the second of each.
        two = self.catalogue["families"] + [dict(row, id=row["id"] + "-2") for row in self.catalogue["families"]]
        rows = self.ordered(two, facts, rotated)
        self.assertEqual([row["category"] for row in rows], rotated + rotated)
        same = [family("a-first", "A", parent="branch-x"), family("b-second", "B", parent="branch-y"),
                family("c-third", "C", parent="branch-x"), family("d-fourth", "D", parent="branch-y"),
                family("e-fifth", "E", parent="branch-z")]
        facts["branch_served"] = {"branch-x": 3, "branch-y": 1}
        rows = self.ordered(same, facts)
        self.assertEqual(rows[0]["id"], "e-fifth")
        self.assertEqual({row["id"] for row in rows[1:3]}, {"b-second", "d-fourth"})
        many = [family("heading-" + chr(97 + n), "H") for n in range(12)]
        facts["branch_served"] = {}
        ids = [row["id"] for row in self.ordered(many, facts)]
        self.assertNotEqual(ids, sorted(ids))
        self.assertEqual(ids, [row["id"] for row in self.ordered(list(reversed(many)), facts)])

    def test_heading_facts_read_the_saved_signal_file(self):
        import tempfile
        from product_radar.map_materials import FOLDER, SIGNALS
        with tempfile.TemporaryDirectory() as folder:
            atomic_json(Path(folder) / FOLDER / SIGNALS, {"version": 1, "rows": [
                {"words": ["onion", "chopper", "bowl"], "at": AT, "kind": "platform-item"}]})
            records = state(batch_runs={"w": wave("w", "2026-10-01T00:00:00+00:00", [("chopper", "q", None)])})
            facts = order.heading_facts(records, self.catalogue, root=folder)
        self.assertEqual((facts["served"], facts["fresh"], facts["crowded"], facts["branch_served"]),
                         ({"chopper": "2026-10-01T00:00:00+00:00"}, {"chopper": 1}, set(), {"branch-a": 1}))


class CrowdingSettingsTests(unittest.TestCase):
    def test_unreadable_crowding_settings_leave_crowding_unknown(self):
        records = state(observations={"o": {"acquisition_id": "acq", "semantic_id": "s"}},
                        associations={"x": {"status": "resolved", "semantic_id": "s", "concept_id": "c"}},
                        concepts={"c": {}}, batch_runs={key("crowding-fact", "c"): {"level": "high"}})
        with patch("product_radar.crowding.load_thresholds", side_effect=SafetyError("CROWDING_SETTINGS_INVALID")):
            self.assertEqual(order.crowded_headings(records, {"acq": "a"}), set())
        with patch("product_radar.crowding.load_thresholds", side_effect=SafetyError("RESEARCH_EVENTS_INVALID")):
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
                order.crowded_headings(records, {"acq": "a"})


class WaveOrderTests(unittest.TestCase):
    """The wave preparation uses this order (synthetic Meta leaf, real waves)."""

    def harness(self):
        import test_discovery_waves as waves
        case = waves.DiscoveryWaveTests("test_many_wave_candidates_expand_catalogue_once_and_keep_identical_questions")
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.catalogue = waves.catalogue(3)
        case.catalogue["families"][1].update(label="Draining dish tray", phrases=[waves.phrase("draining dish tray")])
        return case, waves

    def planned(self, case, wave_id):
        return [row["family_id"] for row in case.memory.snapshot()["batch_runs"][wave_id]["planned_actions"]]

    def test_crowded_headings_wait_and_fresh_signals_come_back_first(self):
        case, waves = self.harness()
        with patch.object(order, "crowded_headings", return_value={"caddy-1"}):
            first = case.advance(action_limit=2)
        self.assertNotIn("caddy-1", self.planned(case, first["wave_ids"][0]))
        for _ in range(2):  # the first wave's second search, then the crowded heading once the others are served
            case.advance(action_limit=1)
        served = {fid for row in case.memory.snapshot()["batch_runs"].values() if row.get("kind") == WAVE_VERSION
                  for fid in [action["family_id"] for action in row["planned_actions"]]}
        self.assertEqual(served, {"caddy", "caddy-1", "caddy-2"})
        # A week later every heading is due again; a fresh signal brings caddy-1 back first.
        from product_radar.map_materials import word_set
        atomic_json(case.memory.root / "product_map" / "signals.json", {"version": 1, "rows": [
            {"words": sorted(word_set("steel draining dish tray")), "at": "2026-10-12T00:00:00+00:00",
             "kind": "trends-rising"}]})
        case.now = waves.LATER
        later = case.advance(action_limit=1)
        self.assertEqual(self.planned(case, later["wave_ids"][-1]), ["caddy-1"])  # not first by the spread


class WaveCostTests(unittest.TestCase):
    """Wave preparation looks only at the headings it can use; readiness does no
    candidate work while the current wave is unsettled; one subject per heading."""

    def harness(self, count=12):
        import test_discovery_waves as waves
        case = waves.DiscoveryWaveTests("test_many_wave_candidates_expand_catalogue_once_and_keep_identical_questions")
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.catalogue = waves.catalogue(count)
        return case

    def test_preparation_stops_once_the_wave_can_be_filled(self):
        from product_radar import catalogue_discovery as discovery
        case = self.harness()
        seen = []
        real = discovery._family_specs

        def counted(state_, family, handlers):
            seen.append(family["id"])
            return real(state_, family, handlers)
        with patch.object(discovery, "_family_specs", side_effect=counted):
            wave = discovery.prepare_next_discovery_wave(case.memory, "programme", case.catalogue,
                                                         handlers=case.handlers(), action_limit=2, as_of=case.now)
        self.assertEqual(len(wave["planned_actions"]), 2)
        self.assertEqual(len(seen), 2)  # one category: the first two headings in order fill the wave
        # The full readiness check still sees every heading.
        seen.clear()
        with patch.object(discovery, "_family_specs", side_effect=counted), \
                patch.object(discovery, "_first_pass_settled", return_value=True):
            ready = discovery.discovery_wave_readiness(case.memory, "programme", case.catalogue,
                                                       handlers=case.handlers(), as_of=case.now)
        self.assertEqual(len(seen), 12)
        self.assertTrue(ready["new_wave_ready"])

    def test_one_headings_source_questions_by_phrase_source(self):
        from product_radar import catalogue_discovery as discovery
        from product_radar.supplier_route_acquisition import ACTIVE_SEARCH_ROUTES
        heading = {"id": "h", "phrases": [
            {"text": "steel spice rack", "source": source, "purpose": purpose, "country": "IN"}
            for source, purpose in (("meta", "discovery"), ("marketplace", "validation"), ("trends", "validation"),
                                    ("supplier", "discovery"), ("quickcommerce", "validation"), ("dropship", "validation"))]}
        handlers = {route: {"prepare_source": lambda context: context["request"]} for route in ACTIVE_SEARCH_ROUTES[:1]}
        rows = list(discovery._family_specs({"query_tasks": {}, "batch_runs": {}, "source_checks": {}}, heading, handlers))
        self.assertEqual([(row[1], row[4]) for row in rows],
                         [("meta-ads", "discovery"), ("google-trends", "validation"), (ACTIVE_SEARCH_ROUTES[0], "sourcing")])

    def test_readiness_skips_candidate_work_while_the_current_wave_is_unsettled(self):
        from product_radar import catalogue_discovery as discovery
        case = self.harness(3)
        case.advance(action_limit=3)  # a wave with an unsettled first pass
        with patch.object(discovery, "_wave_candidates", side_effect=AssertionError("not needed")):
            ready = discovery.discovery_wave_readiness(case.memory, "programme", case.catalogue,
                                                       handlers=case.handlers(), as_of=case.now)
        self.assertFalse(ready["new_wave_ready"])
        self.assertEqual(ready["signal"][-1][2], [])

    def test_one_subject_per_heading_in_an_enumeration(self):
        from product_radar import research_questions as questions
        from product_radar.catalogue_expansion import effective_catalogue
        case = self.harness(2)
        state_ = case.memory.snapshot()
        current = effective_catalogue(case.catalogue, state_)
        with questions._catalogue_direction_scope(state_, case.catalogue, current), \
                patch.object(questions, "_accepted_family", side_effect=AssertionError("scope view only")):
            first = questions.catalogue_direction_subject(state_, "caddy", catalogue=case.catalogue)
            first["scope"]["included"].append("caller edit")
            again = questions.catalogue_direction_subject(state_, "caddy", catalogue=case.catalogue)
            other = questions.catalogue_direction_subject(state_, "caddy", catalogue=case.catalogue, qualifiers={"x": "y"})
            with self.assertRaisesRegex(SafetyError, "RESEARCH_DIRECTION_NOT_ACCEPTED"):
                questions.catalogue_direction_subject(state_, "unknown", catalogue=case.catalogue)
        self.assertNotIn("caller edit", again["scope"]["included"])
        self.assertEqual(again, questions.catalogue_direction_subject(state_, "caddy", catalogue=case.catalogue))
        self.assertNotEqual(other["scope_hash"], again["scope_hash"])


if __name__ == "__main__":
    unittest.main()
