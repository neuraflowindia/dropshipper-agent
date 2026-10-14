"""Bulk installed programme readiness proof, wholly synthetic and local."""

from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from product_radar.marketplace_browser import build_marketplace_request
from product_radar.programme import _source_turn_readiness, prepare_programme
from product_radar.source_acquisitions import claim_source_acquisition
from product_radar.source_coverage import ROUTES, installed_source_handlers, prepare_source_tasks
from product_radar.storage import atomic_json
from test_quickcommerce import LOCATION
import test_source_access_waits as source_fixtures


NOW = source_fixtures.NOW


def forbidden(*_args, **_kwargs):
    raise AssertionError("Readiness must not call any actual reader")


class ProgrammeReadinessSnapshotReviewTests(TestCase):
    def setUp(self):
        self.source = source_fixtures.SourceAccessWaitTests()
        self.source.setUp()
        self.addCleanup(self.source.doCleanups)
        self.memory = self.source.memory
        self.handlers = installed_source_handlers(self.memory,
            marketplace_options={"shared_acquisitions": True, "capture": forbidden},
            supplier_options={"shared_acquisitions": True, "respect_access_waits": True,
                              "http_reader": forbidden, "page_reader": forbidden, "browser_reader": forbidden},
            trends_options={"shared_acquisitions": True, "capture": forbidden},
            meta_options={"shared_acquisitions": True, "browser": forbidden},
            quickcommerce_options={"shared_acquisitions": True, "respect_access_waits": True,
                                   "native_capture": forbidden})
        self.rows = []
        for route in ROUTES:
            if route in {"amazon-bestsellers", "amazon-movers-and-shakers"}:
                requests = [build_marketplace_request(route, department="home-and-kitchen")]
            else:
                requests = []
                for index in range(3):
                    term = "draining sink caddy " + str(index)
                    if route in {"blinkit", "zepto", "instamart"}:
                        request = {"query": term, "location": deepcopy(LOCATION)}
                    elif route == "company-pages":
                        # Nominations are only candidate URLs here, not confirmed
                        # supplier evidence or permission to read the page.
                        request = {"candidate_urls": {"sink": ["https://factory.example/sink-caddy-" + str(index)]},
                                   "candidate_check_ids": {"sink": []}}
                    else:
                        request = {"query": term, "country": "IN"}
                    requests.append(request)
            self.rows.extend({"route": route, "request": request, "concept_ids": ["sink"]}
                             for request in requests)
        self.prepared = prepare_source_tasks(self.memory, self.rows, run_id="bulk-readiness", as_of=NOW)
        self.record = prepare_programme(self.memory, "bulk-programme", source_run_id="bulk-readiness",
                                        concept_ids=["sink"], location=LOCATION, as_of=NOW)

    def canonical_files(self):
        return {str(path.relative_to(self.memory.root)): path.read_bytes()
                for path in self.memory.root.rglob("*.json")
                if "direct_marketplace" not in path.parts}

    def standalone_handlers(self):
        return {route: {name: callback for name, callback in value.items() if not name.endswith("_from_state")}
                for route, value in self.handlers.items()}

    def assert_readiness_parity(self):
        state = self.memory.snapshot(); before = deepcopy(state); files = self.canonical_files()
        with patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as standalone_reads:
            expected = _source_turn_readiness(self.memory, state, self.record, self.standalone_handlers(), NOW)
        # Two standalone hooks per unfinished task previously each obtained a
        # full canonical snapshot, even though programme already supplied one.
        # Saved questions of the retired Google Search route have no hooks.
        self.assertEqual(standalone_reads.call_count,
                         2 * len([row for row in self.rows if row["route"] != "google-suppliers"]))
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("Nested whole-history read")):
            actual = _source_turn_readiness(self.memory, state, self.record, self.handlers, NOW)
        self.assertEqual(actual, expected)
        self.assertEqual(state, before)
        self.assertEqual(self.canonical_files(), files)
        return actual

    def test_bulk_all_fourteen_installed_routes_reuse_verified_state_without_writes(self):
        self.assertEqual(len(self.handlers), 14)
        self.assertEqual(len(self.rows), 38)
        state = self.memory.snapshot()
        tasks = [state["query_tasks"][ref] for ref in self.prepared["task_ids"]]
        self.assertEqual({row["route"] for row in tasks}, set(ROUTES))
        self.assertTrue(all(row["status"] == "prepared" and row["tries"] == 0 for row in tasks))
        result = self.assert_readiness_parity()
        self.assertTrue(result["callback_ready"])
        self.assertEqual(len(result["signal"]), 38)
        retired = [row for row in result["signal"] if state["query_tasks"][row[0]]["route"] == "google-suppliers"]
        self.assertEqual(len(retired), 3)
        self.assertFalse(any(row[2] for row in retired))  # Never a callback for the retired route.
        self.assertEqual(self.source.calls, [])

    def test_bulk_saved_host_refusal_preserves_healthy_siblings_and_exact_advice(self):
        run = self.source.prepare("prior-host-refusal", "amazon-search", term="unrelated basket")
        self.source.execute(run)
        self.assertEqual(len(self.source.calls), 1)
        result = self.assert_readiness_parity()
        self.assertTrue(result["callback_ready"])
        self.assertIn(source_fixtures.DUE, result["waits"])
        state = self.memory.snapshot()
        by_route = {state["query_tasks"][row[0]]["route"]: row[5] for row in result["signal"]}
        self.assertFalse(by_route["amazon-search"]["ready"])
        self.assertFalse(by_route["amazon-bestsellers"]["ready"])
        self.assertTrue(by_route["flipkart-search"]["ready"])
        self.assertEqual(len(self.source.calls), 1)

    def test_late_original_file_wakes_actual_bulk_programme_without_new_canonical_state(self):
        initial = self.memory.snapshot()
        task_id = next(ref for ref in self.prepared["task_ids"]
                       if initial["query_tasks"][ref]["route"] == "amazon-search")
        task = initial["query_tasks"][task_id]
        request = build_marketplace_request("amazon-search", term=task["request"]["query"])
        owner = claim_source_acquisition(self.memory, "amazon-search", request, owner_id=task_id,
            as_of=NOW, artifact_roots=[self.source.source_root])
        state = self.memory.snapshot(); original = deepcopy(state); files = self.canonical_files()
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("Late-file nested whole-history read")):
            before = _source_turn_readiness(self.memory, state, self.record, self.handlers, NOW)
            atomic_json(self.source.source_root / owner["acquisition_id"] / "native_result.json",
                        {"synthetic": "New original child bytes, not yet accepted evidence."})
            after = _source_turn_readiness(self.memory, state, self.record, self.handlers, NOW)
        self.assertNotEqual(before["signal"], after["signal"])
        before_hint = next(row[4] for row in before["signal"] if row[0] == task_id)
        after_hint = next(row[4] for row in after["signal"] if row[0] == task_id)
        self.assertEqual(before_hint["acquisition_id"], owner["acquisition_id"])
        self.assertEqual(after_hint["acquisition_id"], owner["acquisition_id"])
        self.assertFalse(before_hint["files"])
        self.assertTrue(any(row[0] == "native_result.json" for row in after_hint["files"]))
        self.assertEqual(state, original)
        self.assertEqual(self.canonical_files(), files)
        self.assertEqual(self.source.calls, [])

    def assert_disabled_due_keeps_access_fallback(self, optional_pair):
        handlers = {route: dict(value) for route, value in self.handlers.items()}
        calls = []
        def access(context):
            calls.append(context["task_id"])
            return {"ready": False, "local_saved": False, "reason": "Existing access fallback wait"}
        handlers["amazon-search"].update(marketplace_due_hint=None, source_access_hint=access,
            marketplace_due_hint_from_state=optional_pair)
        state = self.memory.snapshot(); before = deepcopy(state); files = self.canonical_files()
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("Disabled-hook fallback nested snapshot")):
            result = _source_turn_readiness(self.memory, state, self.record, handlers, NOW)
        tasks = {ref for ref in self.prepared["task_ids"] if state["query_tasks"][ref]["route"] == "amazon-search"}
        self.assertEqual(set(calls), tasks)
        self.assertEqual(len(calls), 3)
        for row in result["signal"]:
            if row[0] in tasks:
                self.assertEqual(row[5]["reason"], "Existing access fallback wait")
        self.assertTrue(result["callback_ready"])
        self.assertEqual(state, before)
        self.assertEqual(self.canonical_files(), files)
        self.assertEqual(self.source.calls, [])

    def test_disabled_installed_due_with_retained_factory_pair_uses_access_fallback(self):
        self.assert_disabled_due_keeps_access_fallback(
            self.handlers["amazon-search"]["marketplace_due_hint_from_state"])

    def test_disabled_due_with_malformed_orphan_optional_pair_uses_access_fallback(self):
        self.assert_disabled_due_keeps_access_fallback("Unused optional optimization metadata")

    def test_valid_original_base_ignores_malformed_or_mismatched_optional_pair(self):
        state = self.memory.snapshot(); before = deepcopy(state); files = self.canonical_files()
        expected = _source_turn_readiness(self.memory, state, self.record, self.handlers, NOW)
        for optional_pair in ("Unused optional optimization metadata", forbidden):
            with self.subTest(optional_pair=optional_pair):
                handlers = {route: dict(value) for route, value in self.handlers.items()}
                handlers["amazon-search"]["marketplace_due_hint_from_state"] = optional_pair
                with patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as snapshots:
                    actual = _source_turn_readiness(self.memory, state, self.record, handlers, NOW)
                # Only these three due callbacks take their standalone path;
                # all remaining valid pairs still reuse the supplied snapshot.
                self.assertEqual(snapshots.call_count, 3)
                self.assertEqual(actual, expected)
        self.assertEqual(state, before)
        self.assertEqual(self.canonical_files(), files)
        self.assertEqual(self.source.calls, [])

    def test_malformed_actual_base_still_fails_existing_handler_validation(self):
        from product_radar.models import SafetyError
        handlers = {route: dict(value) for route, value in self.handlers.items()}
        handlers["amazon-search"]["marketplace_due_hint"] = "Invalid actual base callback"
        state = self.memory.snapshot(); before = deepcopy(state); files = self.canonical_files()
        with self.assertRaisesRegex(SafetyError, "SOURCE_MARKETPLACE_HOOK_INVALID"):
            _source_turn_readiness(self.memory, state, self.record, handlers, NOW)
        self.assertEqual(state, before)
        self.assertEqual(self.canonical_files(), files)
        self.assertEqual(self.source.calls, [])

