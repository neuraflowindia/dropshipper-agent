"""Three readiness readers sharing one verified state; offline fixtures only.

Run with --measure-baseline only on the pre-change tree to count its nine-scope
nested reads. Conditional model retry readers retain their existing authority.
"""

from copy import deepcopy
import json
import sys
import unittest
from unittest.mock import patch

import product_radar.catalogue_scope_review as scope
import product_radar.functional_definition_review as definition
import product_radar.functional_example_review as example
import product_radar.programme as programme
from product_radar.models import SafetyError, digest
from product_radar.research_ai import Jobs, current_attempt
from product_radar.storage import atomic_json
import test_catalogue_expansion as catalogue_fixtures
import test_catalogue_scope_review as scope_fixtures
import test_functional_definition_review as definition_fixtures
import test_functional_example_review as example_fixtures
import test_programme as programme_fixtures


def nine_scope_fixture():
    fixture = programme_fixtures.ProgrammeTests()
    fixture.setUp()
    record = fixture.prepare()
    for index in range(8):
        cid = "new-independent-" + str(index)
        fixture.product(cid)
        programme._register_discovered_products(fixture.memory, record, {"new_concept_ids": [cid]}, fixture.now)
    programme._save(fixture.memory, record)
    assert len(programme._review_scopes(record)) == 9
    return fixture, record


def discovery_step():
    def step(*_args):
        raise AssertionError("Readiness must not run discovery")
    step.discovery_catalogue = catalogue_fixtures.fixture()
    return step


def measure_baseline():
    fixture, record = nine_scope_fixture()
    try:
        state = fixture.memory.snapshot()
        with patch.object(fixture.memory, "snapshot", wraps=fixture.memory.snapshot) as snapshots:
            programme.programme_turn_readiness(fixture.memory, record, root=fixture.root,
                handlers={}, discovery_step=discovery_step(), as_of=fixture.now, state=state)
        result = {"scopes": 9, "verified_state_supplied": True, "nested_snapshot_calls": snapshots.call_count,
                  "expected_current_baseline": 28, "expected_candidate": 0,
                  "provider_calls": 0, "live_model_calls": 0}
        print(json.dumps(result, sort_keys=True))
        assert snapshots.call_count == 28, result
    finally:
        fixture.doCleanups()


class ProgrammeReadinessSuppliedStateTests(unittest.TestCase):
    def fixture(self, fixture_class):
        value = fixture_class()
        value.setUp()
        self.addCleanup(value.doCleanups)
        return value

    def fixtures(self):
        d = self.fixture(definition_fixtures.FunctionalDefinitionReviewTests)
        e = self.fixture(example_fixtures.FunctionalExampleReviewTests)
        s = self.fixture(scope_fixtures.CatalogueScopeTests)
        return [
            ("definition", d, definition.functional_definition_readiness,
             {"concept_ids": [d.cids[0]], "as_of": definition_fixtures.NOW}),
            ("example", e, example.functional_example_readiness,
             {"concept_ids": [e.cid], "as_of": example_fixtures.NOW}),
            ("scope", s, scope.catalogue_scope_readiness,
             {"concept_ids": [s.cid], "catalogue": s.catalogue, "as_of": scope_fixtures.waves.NOW,
              "runner_available": True}),
        ]

    def test_actual_nine_scope_programme_readiness_reuses_its_supplied_state(self):
        fixture, record = nine_scope_fixture()
        self.addCleanup(fixture.doCleanups)
        state = fixture.memory.snapshot()
        frozen = deepcopy(state)
        options = {"root": fixture.root, "handlers": {}, "discovery_step": discovery_step(),
                   "as_of": fixture.now, "state": state}
        def default_fresh(reader):
            def standalone(*args, state=None, **kwargs):
                return reader(*args, **kwargs)
            return standalone
        # Compare the real programme with three default fresh readers against
        # the real programme with their supplied-state path, not two invocations
        # of the same new path. Files are still inspected by both paths.
        with patch.object(scope, "catalogue_scope_readiness", default_fresh(scope.catalogue_scope_readiness)), \
             patch.object(definition, "functional_definition_readiness", default_fresh(definition.functional_definition_readiness)), \
             patch.object(example, "functional_example_readiness", default_fresh(example.functional_example_readiness)):
            expected = programme.programme_turn_readiness(fixture.memory, record, **options)
        with patch.object(fixture.memory, "snapshot", side_effect=AssertionError("Nested whole-store read")):
            actual = programme.programme_turn_readiness(fixture.memory, record, **options)
        self.assertEqual(actual, expected)
        self.assertEqual(state, frozen)
        self.assertEqual(fixture.memory.snapshot(), frozen)

    def test_all_three_supplied_state_readers_exactly_match_default_without_nested_reads(self):
        for name, fixture, reader, options in self.fixtures():
            with self.subTest(reader=name):
                state = fixture.memory.snapshot()
                frozen = deepcopy(state)
                expected = reader(fixture.memory, **options)
                with patch.object(fixture.memory, "snapshot", side_effect=AssertionError("Nested whole-store read")):
                    actual = reader(fixture.memory, state=state, **options)
                self.assertEqual(actual, expected)
                self.assertEqual(state, frozen)

    def test_standalone_defaults_still_verify_fresh_state_on_every_call(self):
        for name, fixture, reader, options in self.fixtures():
            with self.subTest(reader=name):
                with patch.object(fixture.memory, "snapshot", wraps=fixture.memory.snapshot) as snapshots:
                    first = reader(fixture.memory, **options)
                    first_reads = snapshots.call_count
                    second = reader(fixture.memory, **options)
                self.assertGreaterEqual(first_reads, 1)
                self.assertGreaterEqual(snapshots.call_count - first_reads, 1)
                self.assertEqual(first, second)

    def original(self, memory, payload, at):
        # Jobs with no runner can register the existing contract but cannot
        # launch anything. A synthetic intent then models an unknown original.
        with self.assertRaisesRegex(SafetyError, "AI_RUNNER_NOT_ENABLED"):
            Jobs(memory, None).run("B", payload)
        state = memory.snapshot()
        version = definition.PROMPT_VERSION if "definition_contract" in payload else (
            example.PROMPT_VERSION if "example_contract" in payload else scope.PROMPT_VERSION)
        jid, job = next((jid, job) for jid, job in state["jobs"].items()
                        if job["request"]["prompt_version"] == version)
        folder = current_attempt(memory.root / "ai_jobs" / jid)
        atomic_json(folder / "intent.json", {"request": job["request"], "created_at": at})
        return jid, job["request"], folder

    def same_state_late_file(self, fixture, reader, options, payload):
        jid, request, folder = self.original(fixture.memory, payload, options["as_of"])
        state = fixture.memory.snapshot()
        frozen = deepcopy(state)
        # Conditional closed-retry readers deliberately retain their own
        # snapshot call. Feed that call the same canonical state here; the
        # target is live native-file wake, not a retry-authority change.
        with patch.object(fixture.memory, "snapshot", return_value=state):
            waiting = reader(fixture.memory, state=state, **options)
        self.assertFalse(waiting["local_ready"])
        self.assertFalse(waiting["ready"])
        self.assertEqual(state, frozen)
        atomic_json(folder / "result.json", {"request_hash": digest(request), "response": {},
            "usage": None, "completed_at": options["as_of"]})
        # Arrival is a wake hint, not acceptance of this intentionally invalid
        # response. Actual review remains responsible for original validation.
        with patch.object(fixture.memory, "snapshot", return_value=state):
            late = reader(fixture.memory, state=state, **options)
        self.assertTrue(late["local_ready"])
        self.assertNotEqual(waiting["signal"], late["signal"])
        self.assertNotEqual(waiting["bindings"], late["bindings"])
        self.assertEqual(state, frozen)
        self.assertEqual(fixture.memory.snapshot(), frozen)
        self.assertIn(jid, frozen["jobs"])

    def test_definition_same_state_still_observes_a_late_original_file(self):
        fixture = self.fixture(definition_fixtures.FunctionalDefinitionReviewTests)
        options = {"concept_ids": [fixture.cids[0]], "as_of": definition_fixtures.NOW}
        item = definition.definition_review_input(fixture.memory.snapshot(), fixture.cids[0],
                                                 as_of=definition_fixtures.NOW)["item"]
        payload = {"mode": definition.MODE, "definition_contract": definition.CONTRACT, "items": [item]}
        self.same_state_late_file(fixture, definition.functional_definition_readiness, options, payload)

    def test_example_same_state_still_observes_a_late_original_file(self):
        fixture = self.fixture(example_fixtures.FunctionalExampleReviewTests)
        options = {"concept_ids": [fixture.cid], "as_of": example_fixtures.NOW}
        item = example.functional_example_readiness(fixture.memory, **options)["prepared"][0]["item"]
        payload = {"mode": example.MODE, "example_contract": example.CONTRACT, "items": [item]}
        self.same_state_late_file(fixture, example.functional_example_readiness, options, payload)

    def test_scope_same_state_still_observes_a_late_original_file(self):
        fixture = self.fixture(scope_fixtures.CatalogueScopeTests)
        options = {"concept_ids": [fixture.cid], "catalogue": fixture.catalogue,
                   "as_of": scope_fixtures.waves.NOW, "runner_available": True}
        item = scope.catalogue_scope_readiness(fixture.memory, **options)["pending"][0]["item"]
        payload = {"mode": scope.MODE, "scope_contract": scope.CONTRACT, "items": [item]}
        self.same_state_late_file(fixture, scope.catalogue_scope_readiness, options, payload)


if __name__ == "__main__":
    if sys.argv[1:] == ["--measure-baseline"]:
        measure_baseline()
    else:
        unittest.main()
