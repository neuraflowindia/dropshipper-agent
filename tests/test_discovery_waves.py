"""Renewable independent discovery through installed synthetic source leaves."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from product_radar.catalogue_discovery import (WAVE_VERSION, advance_discovery_cycle, advance_discovery_waves,
    discovery_wave_readiness, prepare_next_discovery_wave, validate_discovery_wave)
from product_radar.meta_browser import meta_direct_source_handlers
from product_radar.models import SafetyError, digest
import product_radar.programme as programme
from product_radar.research_memory import ResearchMemory
from product_radar.source_acquisitions import ACQUISITION_KIND
from product_radar.source_coverage import create_marketplace_handlers, execute_source_run, prepare_source_tasks
from product_radar.storage import Journal
from product_radar.work_turns import bind_work_turn, close_work_turn, open_work_turn
from test_catalogue_expansion import Runner as ExpansionRunner, fixture, phrase
import test_catalogue_expansion as expansion_fixtures
from test_research_flow import FakeRunner
import test_programme as programme_fixtures
import test_catalogue_seed_discovery as seed_fixtures
from test_catalogue_seed import SeedRunner


NOW = "2026-10-09T10:00:00+00:00"
LATER = "2026-10-17T10:00:00+00:00"


def catalogue(count=1):
    value = fixture()
    for index in range(1, count):
        family = deepcopy(value["families"][0])
        family.update(id="caddy-" + str(index), label="Sink caddy " + str(index),
                      phrases=[phrase("draining sink caddy " + str(index))])
        value["families"].append(family)
    return value


class DiscoveryWaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.memory = ResearchMemory(self.root / "research", Journal(self.root / "finance"), evidence_type="synthetic")
        self.catalogue = catalogue(); self.now = NOW; self.calls = []; self.marketplace_calls = []
        self.mode = {}; self.admissions = []

    def handlers(self):
        """The installed shared Meta leaf (the one discovery route, D4) with a
        synthetic browser. Mode "meta-ads" applies to every query; a query's
        own mode overrides it. Amazon and Flipkart stay installed and unused."""
        def browser(url, expression, folder, **options):
            query = parse_qs(urlsplit(url).query)["q"][0]
            self.calls.append((Path(folder).name, {"route": "meta-ads", "query": query}))
            mode = self.mode.get(query, self.mode.get("meta-ads", "empty"))
            if isinstance(mode, Exception):
                raise mode
            ad = str(100000 + int(digest(query)[:5], 16))
            snapshot = {"url": url, "title": "Meta Ad Library", "text": "Ad Library", "text_truncated": False,
                "cards": [{"ad_id": ad, "text": "Active\nLibrary ID: " + ad + "\nStarted running on Oct 1, 2026\n"
                           + query.capitalize() + " pack of 2", "links": [], "images": []}]}
            if mode in ("empty", "blocked"):
                snapshot.update(cards=[], text="Too many requests" if mode == "blocked" else "No ads match this search")
            return {"snapshot": snapshot, "navigation_started": True, "navigation_started_at": self.now,
                    "navigation_status": 429 if mode == "blocked" else 200, "owned_process_exited": True,
                    "completed_at": self.now, "navigation_retry_after": None}
        def marketplace(request, acquisition_id, **options):
            self.marketplace_calls.append(deepcopy(request))
            raise AssertionError("Amazon and Flipkart are each type's crowding check, never discovery")
        handlers = create_marketplace_handlers(self.memory, data_root=self.memory.root / "direct_marketplace",
                                                capture=marketplace, shared_acquisitions=True)
        handlers["meta-ads"] = meta_direct_source_handlers(self.memory, browser=browser, shared_acquisitions=True)
        return handlers

    def no_admission(self, *_args, **options):
        self.admissions.append(deepcopy(options))

    def advance(self, handlers=None, **options):
        handlers = self.handlers() if handlers is None else handlers
        turn = open_work_turn(self.memory, "programme", at=self.now,
                              ready_lanes={"backlog": True, "exploration": True})
        try:
            with bind_work_turn(self.memory, turn["id"]):
                result = advance_discovery_waves(self.memory, "programme", self.catalogue,
                    handlers=handlers, as_of=self.now,
                    admission=options.pop("admission", self.no_admission), **options)
            return result
        finally:
            self.last_turn = close_work_turn(self.memory, turn["id"], at=self.now, reason="Synthetic finite test turn")

    def waves(self):
        return [row for row in self.memory.snapshot()["batch_runs"].values() if row.get("kind") == WAVE_VERSION]

    def owners(self):
        return [row for row in self.memory.snapshot()["batch_runs"].values() if row.get("kind") == ACQUISITION_KIND]

    def ready(self, handlers=None, **options):
        return discovery_wave_readiness(self.memory, "programme", self.catalogue,
            handlers=self.handlers() if handlers is None else handlers, as_of=self.now, **options)

    def test_many_wave_candidates_expand_catalogue_once_and_keep_identical_questions(self):
        from product_radar.catalogue_discovery import _wave_candidates, _wave_candidates_in_scope
        from product_radar.catalogue_expansion import effective_catalogue
        state = self.memory.snapshot(); base = catalogue(3); handlers = self.handlers()
        current = effective_catalogue(base, state)
        expected = _wave_candidates_in_scope(state, "programme", base, current, handlers, NOW,
            cache_days=7)
        with patch("product_radar.catalogue_discovery.effective_catalogue", wraps=effective_catalogue) as outer, \
                patch("product_radar.catalogue_expansion.effective_catalogue", wraps=effective_catalogue) as repeated:
            actual = _wave_candidates(state, "programme", base, handlers, NOW,
                cache_days=7)
            self.assertEqual(outer.call_count, 1)
            self.assertEqual(repeated.call_count, 0)
        self.assertEqual(actual, expected)
        self.assertGreaterEqual(len(actual[1]), 3)
        self.assertEqual(self.calls, [])
        self.assertEqual(state, self.memory.snapshot())

    def test_wave_scope_does_not_leak_into_later_question_validation(self):
        from product_radar.catalogue_discovery import _wave_candidates
        from product_radar.catalogue_expansion import effective_catalogue
        from product_radar.research_questions import catalogue_direction_subject
        state = self.memory.snapshot(); base = catalogue(); handlers = self.handlers()
        _wave_candidates(state, "programme", base, handlers, NOW, cache_days=7)
        with patch("product_radar.catalogue_expansion.effective_catalogue", wraps=effective_catalogue) as repeated:
            catalogue_direction_subject(state, base["families"][0]["id"], catalogue=base)
            self.assertEqual(repeated.call_count, 1)

    def test_actual_wave_preparation_reuses_owner_validation_only_inside_pure_candidates(self):
        from product_radar import source_acquisitions
        from product_radar.catalogue_discovery import _wave_candidates
        for index in range(2):
            source_acquisitions.claim_source_acquisition(self.memory, "amazon-search",
                {"query": "earlier fixture " + str(index), "country": "IN", "limit": 30},
                owner_id="earlier-" + str(index), as_of=NOW)
        state = self.memory.snapshot(); handlers = self.handlers(); base = catalogue(3)
        with patch("product_radar.source_acquisitions._record", wraps=source_acquisitions._record) as checked:
            result = _wave_candidates(state, "programme", base, handlers, NOW, cache_days=7)
            self.assertGreaterEqual(len(result[1]), 3)
            self.assertEqual(checked.call_count, 2)
            source_acquisitions._owners(state, "a" * 64)
            self.assertEqual(checked.call_count, 4, "Actual later actions cannot borrow the candidate scope")
        self.assertEqual(state, self.memory.snapshot())
        self.assertEqual(self.calls, [])

    def test_actual_pending_interpretation_uses_one_pre_admission_prefix_and_refreshes_after(self):
        from product_radar import catalogue_discovery
        from product_radar.operations import operation_run, operation_writer, read_operation_status, record_event
        self.catalogue = catalogue(3); self.mode["meta-ads"] = "usable"
        original = catalogue_discovery._acquisition_progress
        reads = []
        admitted = []
        def progress(state, acquisitions, baseline):
            reads.append((bool(admitted), state, list(acquisitions)))
            return original(state, acquisitions, baseline)
        def admit(*_args, **options):
            self.assertEqual(read_operation_status(self.root)["message"], "Reviewing products from saved search results.")
            admitted.append(options)
            return {"preparation_waits": [{"stage": "description", "reason": "WORK_TURN_MODEL_LIMIT", "item_count": 2}]}
        with patch.object(catalogue_discovery, "_acquisition_progress", side_effect=progress), \
                operation_run(self.root), operation_writer():
            record_event("stage_started", stage="discovery")
            advanced = self.advance(max_actions=3, admission=admit)
            self.assertEqual(read_operation_status(self.root)["counts"]["completed_results"], 0)
            self.assertEqual(read_operation_status(self.root)["counts"]["deferred"], 1)
            saved = self.memory.snapshot()["batch_runs"][advanced["waves"][0]["wave_id"]]
            self.assertEqual(saved["admission"]["state"], "advanced_with_waits")
            self.assertEqual(saved["admission"]["preparation_waits"],
                [{"stage": "description", "reason": "WORK_TURN_MODEL_LIMIT", "item_count": 2}])
        self.assertEqual(len(admitted), 1)
        before = [state for after, state, _ in reads if not after]
        after = [state for finished, state, _ in reads if finished]
        # Meta is the one discovery route (D4), read once per round: two
        # pre-admission reads still share the one prefix state.
        self.assertGreaterEqual(len(before), 2)
        self.assertTrue(all(state is before[0] for state in before))
        self.assertTrue(after)
        self.assertTrue(all(state is not before[0] for state in after))

    def test_malformed_admission_wait_records_the_specific_invalid_result_reason(self):
        self.mode["meta-ads"] = "usable"
        def admit(*_args, **_options):
            return {"preparation_waits": [{"stage": [], "reason": "WORK_TURN_MODEL_LIMIT", "item_count": 2}]}
        advanced = self.advance(max_actions=3, admission=admit)
        saved = self.memory.snapshot()["batch_runs"][advanced["waves"][0]["wave_id"]]
        self.assertEqual(saved["admission"]["state"], "waiting")
        self.assertEqual(saved["admission"]["reason"], "DISCOVERY_ADMISSION_RESULT_INVALID")
        self.assertEqual(len(self.calls), 1)

    def test_standalone_readiness_scopes_old_question_owner_hints_and_does_not_leak(self):
        from product_radar import source_acquisitions
        from product_radar import catalogue_discovery
        handlers = self.handlers()
        wave = prepare_next_discovery_wave(self.memory, "programme", self.catalogue,
            handlers=handlers, as_of=self.now)
        for index in range(2):
            source_acquisitions.claim_source_acquisition(self.memory, "amazon-search",
                {"query": "old original " + str(index), "country": "IN", "limit": 30},
                owner_id="previous-" + str(index), as_of=NOW)
        state = self.memory.snapshot()
        frozen = deepcopy(state)
        original = catalogue_discovery._wave_question_hint
        def hint(*args, **options):
            # Exercise the same owner lookup before the candidate enumeration,
            # which has its own separate advisory scope.
            source_acquisitions._owners(options["state"], "a" * 64)
            return original(*args, **options)
        with patch.object(catalogue_discovery, "_wave_candidates", return_value=(self.catalogue, [], [], [])), \
                patch.object(catalogue_discovery, "_wave_question_hint", side_effect=hint), \
                patch.object(source_acquisitions, "_record", wraps=source_acquisitions._record) as checked, \
                patch.object(self.memory, "snapshot", side_effect=AssertionError("nested history replay")):
            result = discovery_wave_readiness(self.memory, "programme", self.catalogue,
                handlers=handlers, as_of=NOW, state=state)
            self.assertEqual(len(result["work"]), len(wave["planned_actions"]))
            self.assertEqual(checked.call_count, 2)
            source_acquisitions._owners(state, "a" * 64)
            self.assertEqual(checked.call_count, 4, "A later action cannot inherit the readiness view")
        self.assertEqual(state, frozen)
        self.assertEqual(state, self.memory.snapshot())
        self.assertEqual(self.calls, [])

    def admit_family_with_saved_merge(self):
        # Use the actual admission producer, then a genuine later same-family
        # judgment. Static-only families do not reproduce double expansion.
        fixture = expansion_fixtures.ExpansionTests()
        fixture.memory = self.memory; fixture.base = self.catalogue
        fixture.concept()
        first = fixture.run_one(ExpansionRunner())
        family_id = first["records"][0]["family_id"]
        fixture.concept("Magnetic pan lid rail", concept_id="concept2")
        merged = fixture.run_one(ExpansionRunner(lambda row, _item:
            row.update(decision="same_family", match_id=family_id)), ids=["concept2"])
        self.assertEqual(merged["records"][0]["outcome"], "merged")
        return family_id

    def test_admitted_family_with_saved_alias_supports_readiness_and_actual_wave_preparation(self):
        family_id = self.admit_family_with_saved_merge()
        handlers = self.handlers(); before = self.memory.snapshot(); base = deepcopy(self.catalogue)
        hint = self.ready(handlers)
        self.assertTrue(hint["new_wave_ready"])
        self.assertEqual(before, self.memory.snapshot(), "Readiness must not rewrite original additions")
        wave = prepare_next_discovery_wave(self.memory, "programme", self.catalogue,
            handlers=handlers, action_limit=20, as_of=self.now)
        self.assertEqual(validate_discovery_wave(self.memory.snapshot(), wave), wave)
        added = [row for row in wave["planned_actions"] if row["family_id"] == family_id]
        self.assertTrue(added)
        for row in added:
            question = self.memory.snapshot()["query_tasks"][row["questions"][0]["id"]]
            self.assertEqual(question["subject"]["family_id"], family_id)
            self.assertEqual(question["subject"]["scope"]["definition"],
                             before["catalogue_additions"][next(ref for ref, value in before["catalogue_additions"].items()
                                 if value.get("outcome") == "accepted")]["family"]["definition"])
        self.assertEqual(self.memory.snapshot()["catalogue_additions"], before["catalogue_additions"])
        self.assertEqual(self.catalogue, base)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.memory.journal.path.exists())

    def test_admitted_family_conflict_is_still_rejected_without_calls_or_rewriting(self):
        from product_radar.catalogue_expansion import effective_catalogue
        family_id = self.admit_family_with_saved_merge()
        state = self.memory.snapshot(); conflicting = deepcopy(self.catalogue)
        admission = next(row for row in state["catalogue_additions"].values()
                         if row.get("outcome") == "accepted" and row["family_id"] == family_id)
        original = admission["family"]
        source_ids = {row["id"] for row in conflicting["sources"]}
        conflicting["sources"].extend(deepcopy(row) for row in admission["sources"] if row["id"] not in source_ids)
        changed = deepcopy(original); changed["definition"] = "An incompatible original family meaning."
        conflicting["families"].append(changed)
        with self.assertRaisesRegex(SafetyError, "CATALOGUE_ADDITION_FAMILY_CONFLICT"):
            effective_catalogue(conflicting, state)
        with self.assertRaisesRegex(SafetyError, "CATALOGUE_ADDITION_FAMILY_CONFLICT"):
            prepare_next_discovery_wave(self.memory, "programme", conflicting,
                handlers=self.handlers(), as_of=self.now)
        self.assertEqual(self.memory.snapshot(), state)
        self.assertEqual(self.calls, [])

    def test_preparation_is_real_direction_only_idempotent_and_inert(self):
        handlers = self.handlers()
        wave = prepare_next_discovery_wave(self.memory, "programme", self.catalogue,
            handlers=handlers, as_of=self.now)
        before = self.memory.snapshot()
        again = prepare_next_discovery_wave(self.memory, "programme", catalogue(2),
            handlers=handlers, as_of=LATER)
        self.assertEqual(wave, again)
        self.assertEqual(before, self.memory.snapshot())
        self.assertEqual([row["questions"][0]["route"] for row in wave["planned_actions"]], ["meta-ads"])
        self.assertEqual(self.calls, [])
        self.assertEqual(before["concepts"], {})
        self.assertEqual(before["jobs"], {})
        self.assertFalse(self.memory.journal.path.exists())
        self.assertEqual(validate_discovery_wave(before, wave), wave)
        for planned in wave["planned_actions"]:
            saved = before["query_tasks"][planned["questions"][0]["id"]]
            self.assertEqual(saved["kind"], "research-question-v2")
            self.assertEqual(saved["subject"]["kind"], "catalogue_direction")
            self.assertEqual(saved["subject"]["family_id"], "caddy")

    def test_empty_and_usable_answers_are_independent_and_marketplaces_are_not_launched(self):
        self.catalogue = catalogue(2); self.mode["draining sink caddy 1"] = "usable"
        self.advance(max_actions=3)  # One fresh read of the one discovery route per round.
        self.assertEqual(self.last_turn["counters"]["source_callbacks"], 1)
        result = self.advance(max_actions=3)
        self.assertEqual(sorted(request["query"] for _, request in self.calls), ["draining sink caddy 1", "sink caddy"])
        self.assertEqual(self.marketplace_calls, [])
        self.assertEqual(self.last_turn["counters"]["source_callbacks"], 1)
        wave = self.waves()[0]
        states = {row["questions"][0]["request"]["query"]: wave["actions"][row["id"]]["state"] for row in wave["planned_actions"]}
        self.assertEqual(states, {"sink caddy": "empty", "draining sink caddy 1": "interpretation_pending"})
        self.assertEqual(result["new_concept_ids"], [])
        self.assertEqual(result["paid_actor_calls"], 0)

    def test_saved_amazon_and_flipkart_questions_close_without_interpretation_or_reads(self):
        from product_radar import catalogue_discovery
        # A wave saved before D4 still holds Amazon/Flipkart questions, one with
        # saved cards awaiting interpretation and one never served.
        with patch.object(catalogue_discovery, "FREE_ROUTES", ("meta-ads", "amazon-search", "flipkart-search")):
            wave = prepare_next_discovery_wave(self.memory, "programme", self.catalogue, handlers=self.handlers(),
                                               as_of=self.now)
        routes = {row["questions"][0]["route"]: row["questions"][0]["id"] for row in wave["planned_actions"]}
        self.assertEqual(set(routes), {"meta-ads", "amazon-search", "flipkart-search"})
        saved = self.memory.snapshot()["batch_runs"][wave["id"]]
        pending = deepcopy(saved)
        pending["actions"][routes["amazon-search"]] = {"state": "interpretation_pending", "dispatch_started": True,
            "acquisition_id": "old-amazon-original", "updated_at": self.now}
        self.memory.put("batch_runs", wave["id"], pending, previous=saved)
        self.mode["meta-ads"] = "usable"
        for _ in range(3):
            self.advance(max_actions=3)
        actions = self.waves()[0]["actions"]
        for route in ("amazon-search", "flipkart-search"):
            self.assertEqual((actions[routes[route]]["state"], actions[routes[route]]["reason"]),
                             ("retired", "DISCOVERY_ROUTE_RETIRED"))
            self.assertEqual(self.memory.snapshot()["query_tasks"][routes[route]]["progress"]["scheduling"], "settled")
        self.assertEqual(self.marketplace_calls, [])
        self.assertEqual([request["route"] for _, request in self.calls], ["meta-ads"])
        self.assertTrue(self.admissions)
        self.assertTrue(all("old-amazon-original" not in options["acquisition_ids"] for options in self.admissions))
        self.assertEqual(self.ready()["work"], [])

    def test_renewal_reaches_fifth_and_later_actions_then_stops_without_empty_waves(self):
        self.catalogue = catalogue(6)
        for _ in range(10):
            if not self.ready()["new_wave_ready"] and not self.ready()["work"]:
                break
            self.advance(max_actions=3)
        self.assertEqual(len(self.calls), 6)
        waves = self.waves()
        self.assertGreater(len(waves), 1)
        self.assertEqual(sum(len(row["planned_actions"]) for row in waves), 6)
        self.assertEqual([row["wave_sequence"] for row in waves], list(range(1, len(waves) + 1)))
        self.assertEqual([row["previous_wave_id"] for row in waves], [None, *[row["id"] for row in waves[:-1]]])
        before = self.memory.snapshot()
        waiting = prepare_next_discovery_wave(self.memory, "programme", self.catalogue,
            handlers=self.handlers(), as_of=self.now)
        self.assertEqual(waiting["state"], "waiting")
        self.assertEqual(before, self.memory.snapshot())
        self.assertFalse(self.ready()["new_wave_ready"])
        self.assertEqual(self.ready()["work"], [])

    def test_pending_interpretation_does_not_stop_another_direct_question_or_next_wave(self):
        self.catalogue = catalogue(5); self.mode["meta-ads"] = "usable"
        for _ in range(5):
            self.advance(max_actions=3)
        self.assertEqual(len(self.calls), 5)
        self.assertGreater(len(self.waves()), 1)
        self.assertTrue(any(row["progress"]["interpretation"] == "pending"
                            for row in self.memory.snapshot()["query_tasks"].values()))
        self.assertEqual(self.memory.snapshot()["jobs"], {})
        self.assertFalse(self.ready(runner_available=False)["local_ready"])

    def test_local_pending_work_does_not_spend_two_fresh_callback_allowance(self):
        self.catalogue = catalogue(6); self.mode["meta-ads"] = "usable"
        self.advance(max_actions=2)
        self.advance(max_actions=2)
        before = len(self.calls)
        self.advance(max_actions=2)
        # The pending local rows ride along; the one fresh Meta read still runs.
        self.assertEqual(len(self.calls) - before, 1)
        self.assertEqual(self.last_turn["counters"]["source_callbacks"], 1)
        self.assertGreaterEqual(len(self.admissions), 2)

    def test_exhausted_physical_allowance_does_not_grant_local_group_a_fresh_callback(self):
        handlers = self.handlers()
        wave = prepare_next_discovery_wave(self.memory, "programme", self.catalogue,
            handlers=handlers, as_of=self.now)
        question_ids = [row["questions"][0]["id"] for row in wave["planned_actions"]]
        turn = open_work_turn(self.memory, "programme", at=self.now,
            ready_lanes={"backlog": True, "exploration": True})
        try:
            with bind_work_turn(self.memory, turn["id"]):
                result = advance_discovery_cycle(self.memory, wave["id"], handlers=handlers,
                    max_actions=0, question_ids=question_ids, shared_marketplace=True,
                    admission=self.no_admission, as_of=self.now)
            self.assertEqual(result["external_handler_calls_this_round"], 0)
            self.assertEqual(self.calls, [])
        finally:
            closed = close_work_turn(self.memory, turn["id"], at=self.now, reason="Synthetic local-only slice")
        self.assertEqual(closed["counters"]["source_callbacks"], 0)

    def test_uncertain_original_does_not_cancel_a_sibling_or_get_replaced(self):
        self.catalogue = catalogue(2); self.mode["sink caddy"] = TimeoutError("Synthetic original interrupted")
        self.advance(max_actions=3); self.advance(max_actions=3)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.owners()), 2)
        self.advance(max_actions=3)
        self.assertEqual(len(self.calls), 2)
        states = {owner["binding"]["request"]["query"]: owner["state"] for owner in self.owners()}
        self.assertEqual(states["sink caddy"], "dispatching")
        self.assertEqual(states["draining sink caddy 1"], "completed_empty")

    def test_exact_product_check_receipt_answers_new_direction_without_callback(self):
        self.memory.put("concepts", "sink", {"status": "active", "description": {"label": "Sink caddy",
            "category": "Home and Kitchen", "function": "Store sink essentials", "mechanism": "manual",
            "audience": "adult", "features": ["draining"], "brand": "generic_or_small",
            "basis": "observed", "reason": "Synthetic source fixture."}})
        self.catalogue = catalogue(2); handlers = self.handlers()
        prepare_source_tasks(self.memory, [{"route": "meta-ads", "request": {"query": "sink caddy", "country": "IN"},
            "concept_ids": ["sink"]}], run_id="checks", as_of=self.now)
        execute_source_run(self.memory, "checks", handlers=handlers, as_of=self.now,
            bounded_call=lambda callback, context, _timeout: callback(context), shared_marketplace=True)
        self.assertEqual(len(self.calls), 1)
        result = self.advance(handlers, max_actions=3)
        self.assertEqual(len(self.calls), 2)  # Only the different direction's read.
        self.assertEqual(result["external_handler_calls_this_round"], 1)
        self.assertEqual(self.last_turn["counters"]["source_callbacks"], 1)
        wave = self.waves()[0]
        same = next(row["id"] for row in wave["planned_actions"] if row["questions"][0]["request"]["query"] == "sink caddy")
        self.assertTrue(wave["actions"][same]["reused_acquisition"])

    def test_changed_direction_reuses_fresh_original_without_claiming_old_question_answered_it(self):
        self.advance(max_actions=3)
        old_questions = set(self.memory.snapshot()["query_tasks"])
        self.catalogue["families"][0]["definition"] = "Holds dishwashing tools upright beside the sink."
        self.advance(max_actions=3)
        new_questions = set(self.memory.snapshot()["query_tasks"]) - old_questions
        self.assertEqual(len(new_questions), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.owners()), 1)
        self.assertEqual(self.last_turn["counters"]["source_callbacks"], 0)
        self.assertEqual(self.waves()[-1]["previous_wave_id"], self.waves()[0]["id"])

    def test_due_refresh_uses_linked_generation_and_keeps_original_date(self):
        self.advance(max_actions=3)
        old_wave = deepcopy(self.waves()[0]); old_ids = {row["binding"]["acquisition_id"] for row in self.owners()}
        self.now = LATER
        self.advance(max_actions=3)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.owners()), 2)
        self.assertEqual(self.waves()[0], old_wave)
        self.assertEqual(self.waves()[1]["previous_wave_id"], old_wave["id"])
        self.assertEqual({capture["observed_at"] for capture in self.memory.snapshot()["captures"].values()}, {NOW, LATER})
        fresh = [owner for owner in self.owners() if owner["binding"]["acquisition_id"] not in old_ids]
        self.assertTrue(all(owner["binding"]["generation"] == 2 for owner in fresh))

    def test_actual_a_b_jobs_share_turn_budgets_and_admitted_products_are_follow_on_only(self):
        self.mode["meta-ads"] = "usable"
        runner = FakeRunner()
        result = self.advance(max_actions=3, runner=runner, admission=None)
        self.assertGreater(self.last_turn["counters"]["model_executions"], 0)
        self.assertLessEqual(self.last_turn["counters"]["model_executions"], 10)
        self.assertEqual(result["new_concept_ids"], result["new_products_need_full_source_review"])
        self.assertEqual(len(result["new_concept_ids"]), 1)
        self.assertEqual(self.waves()[0]["baseline_concepts"], [])
        self.assertFalse(result["original_product_pool_changed"])

    def test_host_wait_has_real_wake_time_and_due_probe_reuses_same_question(self):
        self.mode["meta-ads"] = "blocked"
        self.advance(max_actions=3)
        ready = self.ready()
        self.assertEqual(ready["work"], [])
        self.assertFalse(ready["new_wave_ready"])
        self.assertIsNotNone(ready["next_retry_at"])
        self.now = ready["next_retry_at"]
        self.advance(max_actions=3)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.waves()), 1)
        self.assertEqual([request["query"] for _, request in self.calls], ["sink caddy", "sink caddy"])
        self.assertEqual(max(owner["binding"]["generation"] for owner in self.owners()), 2)

    def test_due_probe_interruption_does_not_inherit_first_generation_dispatch_or_receipt(self):
        self.mode["meta-ads"] = "blocked"
        self.advance(max_actions=3)
        wave = self.waves()[0]
        qid = wave["planned_actions"][0]["id"]
        original = deepcopy(wave["actions"][qid])
        self.now = self.ready()["next_retry_at"]
        self.mode["meta-ads"] = TimeoutError("Synthetic new probe interrupted")
        self.advance(max_actions=3)
        action = self.waves()[0]["actions"][qid]
        self.assertEqual(len(self.calls), 2)
        self.assertNotEqual(action["acquisition_id"], original["acquisition_id"])
        self.assertIsNone(action["dispatch_started"])
        self.assertEqual(action["state"], "execution_uncertain")
        self.assertNotIn("capture_ids", action)
        self.assertTrue(Path(original["reference"]).exists())
        self.advance(max_actions=3)
        self.assertEqual(len(self.calls), 2)

    def test_original_turn_admission_before_launch_crash_resumes_same_owner(self):
        from product_radar.catalogue_discovery import reserve_source_callback
        def interrupted(*args, **kwargs):
            reserve_source_callback(*args, **kwargs)
            raise RuntimeError("Synthetic local interruption before physical launch")
        with patch("product_radar.catalogue_discovery.reserve_source_callback", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "Synthetic local interruption"):
                self.advance(max_actions=3)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.last_turn["counters"]["source_callbacks"], 1)
        original = self.owners()[0]["binding"]["acquisition_id"]
        self.advance(max_actions=3)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0], original)
        self.assertEqual(len(self.owners()), 1)

    def test_new_waves_carry_no_list_flag_and_older_saved_ones_still_validate(self):
        from product_radar.catalogue_discovery import _binding
        wave = prepare_next_discovery_wave(self.memory, "programme", self.catalogue,
            handlers=self.handlers(), as_of=self.now)
        self.assertEqual(wave["policy"], {"action_limit": 4, "cache_days": 7})
        older = deepcopy(wave); older["policy"]["include_lists"] = False
        older["binding_hash"] = digest(_binding(older))
        self.assertEqual(validate_discovery_wave(self.memory.snapshot(), older), older)
        older["policy"]["include_lists"] = "yes"; older["binding_hash"] = digest(_binding(older))
        with self.assertRaisesRegex(SafetyError, "DISCOVERY_WAVE_INVALID"):
            validate_discovery_wave(self.memory.snapshot(), older)

    def test_binding_corruption_is_rejected_before_collection(self):
        wave = prepare_next_discovery_wave(self.memory, "programme", self.catalogue,
            handlers=self.handlers(), as_of=self.now)
        damaged = deepcopy(wave)
        damaged["planned_actions"][0]["questions"][0]["request"]["limit"] = 1
        damaged["binding_hash"] = digest({name: damaged[name] for name in (
            "kind", "id", "programme_id", "baseline_concepts", "catalogue_hash", "prepared_at", "planned_actions",
            "wave_sequence", "previous_wave_id", "policy")})
        with self.assertRaisesRegex(SafetyError, "DISCOVERY_WAVE_BINDING_CHANGED"):
            validate_discovery_wave(self.memory.snapshot(), damaged)
        self.assertEqual(self.calls, [])

    def test_one_request_local_proof_error_is_parked_without_stopping_healthy_source(self):
        self.catalogue = catalogue(2); handlers = self.handlers()
        def missing(original):
            def hook(context):
                if context["request"]["query"] == "sink caddy":
                    raise SafetyError("SOURCE_MARKETPLACE_LOCAL_PROOF_PENDING")
                return original(context)
            return hook
        for name in ("source_due_hint", "resolve_source"):
            handlers["meta-ads"][name] = missing(handlers["meta-ads"][name])
        self.advance(handlers, max_actions=3)
        self.assertEqual([request["query"] for _, request in self.calls], ["draining sink caddy 1"])
        wave = self.waves()[0]
        parked = next(row["id"] for row in wave["planned_actions"] if row["questions"][0]["request"]["query"] == "sink caddy")
        self.assertEqual(wave["actions"][parked]["state"], "waiting")
        self.assertEqual(wave["actions"][parked]["reason"], "SOURCE_MARKETPLACE_LOCAL_PROOF_PENDING")
        self.assertEqual(self.ready(handlers)["work"], [])

    def test_shared_memory_integrity_failure_is_not_disguised_as_source_wait(self):
        handlers = self.handlers()
        def corrupt(_context):
            raise SafetyError("RESEARCH_EVENTS_INVALID")
        handlers["meta-ads"]["source_due_hint"] = corrupt
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
            self.advance(handlers, max_actions=3)
        self.assertEqual(self.calls, [])


class ProgrammeDiscoveryWaveTests(unittest.TestCase):
    def setUp(self):
        self.sources = DiscoveryWaveTests(); self.sources.setUp()
        self.addCleanup(self.sources.doCleanups)
        self.fixture = programme_fixtures.ProgrammeTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        # Use the existing whole-programme fixture, with its immutable scope,
        # but install only synthetic source leaves and no other stage work.
        self.fixture.prepare()
        self.sources.memory = self.fixture.memory
        self.sources.root = self.fixture.root
        self.sources.now = self.fixture.now
        self.sources.catalogue = catalogue(6)
        self.handlers = self.sources.handlers()
        from product_radar.catalogue_discovery import create_catalogue_discovery_step
        self.step = create_catalogue_discovery_step(self.sources.catalogue,
            admission=self.sources.no_admission, shared_marketplace=True, renewable_waves=True)

    def advance(self, **options):
        return programme.advance_ready_programme_turn(self.fixture.memory, root=self.fixture.root,
            programme_id="whole-programme", as_of=self.sources.now, handlers=self.handlers,
            steps=self.fixture.noops(), discovery_step=self.step, **options)

    def seed(self):
        seed = seed_fixtures.SeedDiscoveryTests(); seed.setUp(); self.addCleanup(seed.doCleanups)
        seed.memory = self.fixture.memory; seed.root = self.fixture.root; seed.base = self.sources.catalogue
        row = seed.prepared(); manifest = seed.manifest(row)
        from product_radar.catalogue_discovery import create_catalogue_discovery_step
        self.step = create_catalogue_discovery_step(seed.base, seed_manifest=manifest,
            admission=self.sources.no_admission, shared_marketplace=True, renewable_waves=True)
        return row, manifest

    def test_existing_programme_continues_past_initial_wave_then_waits_without_empty_turns(self):
        results = []
        for _ in range(20):
            result = self.advance(); results.append(result)
            if not result["round_completed"]:
                break
        self.assertEqual(results[-1]["state"], "all_waiting")
        self.assertEqual(len(self.sources.calls), 6)
        self.assertGreater(len(self.sources.waves()), 1)
        self.assertEqual(programme.load_programme(self.fixture.memory, "whole-programme")["concept_ids"], ["first", "second"])
        before = self.fixture.memory.snapshot()
        self.assertFalse(self.advance()["round_completed"])
        self.assertEqual(before, self.fixture.memory.snapshot())

    def test_future_model_pause_does_not_stop_renewable_direct_discovery(self):
        record = programme.load_programme(self.fixture.memory, "whole-programme")
        record["model_pause"] = {"reason": "AI_RATE_LIMITED", "at": self.sources.now, "next_retry_at": LATER}
        programme._save(self.fixture.memory, record)
        for _ in range(12):
            result = self.advance()
            if not result["round_completed"]:
                break
        self.assertEqual(len(self.sources.calls), 6)
        self.assertEqual({row["counters"]["model_executions"] for row in self.fixture.memory.snapshot()["batch_runs"].values()
                          if row.get("kind") == "work-turn-v1"}, {0})

    def test_original_seed_preparation_file_arrival_wakes_and_applies_before_retry_deadline(self):
        row, _manifest = self.seed()
        original = Path(row["prepared_reference"]); held = original.with_suffix(".held")
        original.rename(held)
        runner = SeedRunner()
        for _ in range(15):
            result = self.advance(runner=runner)
            if not result["round_completed"]:
                break
        self.assertEqual(result["state"], "all_waiting")
        self.assertEqual(len(runner.calls), 0)
        self.assertIsNotNone(result["next_retry_at"])
        before = self.fixture.memory.snapshot()
        held.rename(original)
        self.assertEqual(before, self.fixture.memory.snapshot())
        resumed = self.advance(runner=runner)
        self.assertTrue(resumed["round_completed"])
        self.assertEqual(len(runner.calls), 1)
        seed = programme.load_programme(self.fixture.memory, "whole-programme")["last_results"]["discovery"]["seed"]
        self.assertEqual(seed["counts"]["completed_examples"], 1)
        self.assertEqual(resumed["work_turn"]["counters"]["model_executions"], 1)

    def test_seed_deadline_wakes_without_file_or_canonical_change_and_wait_settles_again(self):
        row, _manifest = self.seed()
        Path(row["prepared_reference"]).rename(Path(row["prepared_reference"]).with_suffix(".held"))
        for _ in range(15):
            result = self.advance()
            if not result["round_completed"]:
                break
        before = self.fixture.memory.snapshot()
        self.sources.now = result["next_retry_at"]
        self.assertEqual(before, self.fixture.memory.snapshot())
        resumed = self.advance()
        self.assertTrue(resumed["round_completed"])
        self.assertEqual(resumed["work_turn"]["counters"]["model_executions"], 0)
        waiting = self.advance()
        self.assertFalse(waiting["round_completed"])


if __name__ == "__main__":
    unittest.main()
