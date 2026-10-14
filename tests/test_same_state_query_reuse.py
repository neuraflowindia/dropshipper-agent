"""One pure decision reuses its own inputs, never a cross-state cache."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from product_radar.models import SafetyError, digest
from product_radar import product_queries, supplier_search
import test_functional_queries as functional_fixture
import test_product_queries as legacy_fixture
import test_shared_supplier_search as supplier_fixture


class SameStateQueryReuseTests(unittest.TestCase):
    def legacy(self):
        fixture = legacy_fixture.ProductQueryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.prepare(legacy_fixture.Runner())
        return fixture

    def functional(self):
        fixture = functional_fixture.FunctionalQueryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.accept()
        fixture.prepare(functional_fixture.Runner())
        return fixture

    def suppliers(self):
        fixture = supplier_fixture.SharedSupplierTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        handlers = fixture.handlers(routes=("google-maps", "tradeindia"))
        for route in ("google-maps", "tradeindia"):
            fixture.prepare(handlers, route=route, run="synthetic-reuse-" + route)
            fixture.checks(handlers)
        return fixture

    def test_legacy_resolution_builds_its_context_once_with_identical_result(self):
        fixture = self.legacy()
        state = fixture.memory.snapshot()
        before = deepcopy(state)
        expected = product_queries.current_query_profile(state, "bar")
        with patch.object(product_queries, "query_profile_input", wraps=product_queries.query_profile_input) as builds:
            actual = product_queries.resolve_query_profile(state, "bar", catalogue=fixture.catalogue)
        self.assertEqual(builds.call_count, 1)
        self.assertEqual(actual, expected)
        self.assertEqual(state, before)
        actual["aliases"].append("Caller-only change")
        self.assertEqual(product_queries.resolve_query_profile(state, "bar"), expected)

    def test_functional_resolution_builds_its_context_once_and_reads_new_state_freshly(self):
        fixture = self.functional()
        old_state = fixture.memory.snapshot()
        expected = product_queries.current_query_profile(old_state, fixture.cid)
        with patch.object(product_queries, "query_profile_input", wraps=product_queries.query_profile_input) as builds:
            actual = product_queries.resolve_query_profile(old_state, fixture.cid, catalogue=fixture.catalogue)
        self.assertEqual(builds.call_count, 1)
        self.assertEqual(actual, expected)
        changed = dict(functional_fixture.DEFINITION, mechanism="Wall-mounted draining tray")
        fixture.accept(changed, at=functional_fixture.LATER)
        new_state = fixture.memory.snapshot()
        self.assertIsNone(product_queries.resolve_query_profile(new_state, fixture.cid, catalogue=fixture.catalogue))
        self.assertEqual(product_queries.resolve_query_profile(old_state, fixture.cid), expected)

    def test_public_saved_profile_reader_still_builds_and_validates_its_own_input(self):
        fixture = self.legacy()
        state = fixture.memory.snapshot()
        with patch.object(product_queries, "query_profile_input", wraps=product_queries.query_profile_input) as builds:
            self.assertIsNotNone(product_queries.current_query_profile(state, "bar"))
        self.assertEqual(builds.call_count, 1)
        with self.assertRaisesRegex(SafetyError, "^KNOWN_CONCEPT_REQUIRED$"):
            product_queries.current_query_profile(state, "missing")

    def test_supplier_applications_resolve_profile_once_per_read_not_once_per_application(self):
        fixture = self.suppliers()
        state = fixture.memory.snapshot()
        before = deepcopy(state)
        description_hash = digest(state["concepts"][fixture.cid]["description"])
        for _ in range(2):
            with patch.object(product_queries, "resolve_query_profile", wraps=product_queries.resolve_query_profile) as resolves:
                checks = supplier_search.current_supplier_search_checks(state, fixture.cid,
                    description_hash=description_hash)
            self.assertEqual(len(checks), 2)
            self.assertEqual(resolves.call_count, 1)
        self.assertEqual(state, before)

    def test_no_matching_supplier_application_does_not_resolve_profile(self):
        fixture = self.functional()
        state = fixture.memory.snapshot()
        description_hash = digest(state["concepts"][fixture.cid]["description"])
        with patch.object(product_queries, "resolve_query_profile", side_effect=AssertionError("No applicable application")):
            self.assertEqual(supplier_search.current_supplier_search_checks(state, fixture.cid,
                description_hash=description_hash), {})

    def test_duplicate_supplier_profile_reuse_does_not_hide_a_damaged_second_application(self):
        fixture = self.suppliers()
        state = fixture.memory.snapshot()
        applications = [row for row in state["source_checks"].values()
                        if row.get("kind") == supplier_search.APPLICATION_KIND]
        self.assertEqual(len(applications), 2)
        # Preserve the application envelope but damage its separately checked
        # original search record. Every application must still be verified.
        state["source_checks"][applications[1]["binding"]["search_check_id"]]["reason"] = "Damaged synthetic original"
        with self.assertRaisesRegex(SafetyError, "^SUPPLIER_SHARED_APPLICATION_INVALID$"):
            supplier_search.current_supplier_search_checks(state, fixture.cid,
                description_hash=digest(state["concepts"][fixture.cid]["description"]))


if __name__ == "__main__":
    unittest.main()
