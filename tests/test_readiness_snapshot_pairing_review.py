"""Independent proof of pure readiness reuse; all source bytes are synthetic."""

from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from product_radar.catalogue_discovery import discovery_wave_readiness
from product_radar.marketplace_browser import build_marketplace_request
from product_radar.models import SafetyError, digest
from product_radar.research_memory import ResearchMemory
from product_radar.source_acquisitions import claim_source_acquisition
from product_radar.source_coverage import source_access_context
from product_radar.storage import atomic_json
import test_source_access_waits as source_fixtures
from test_catalogue_expansion import fixture as catalogue_fixture


NOW = source_fixtures.NOW


class ReadinessSnapshotPairingReviewTests(TestCase):
    def setUp(self):
        self.source = source_fixtures.SourceAccessWaitTests()
        self.source.setUp()
        self.addCleanup(self.source.doCleanups)
        self.memory = self.source.memory
        self.installed = self.source.handlers["amazon-search"]
        self.context = {"route": "amazon-search", "request": {"query": "sink caddy", "country": "IN"},
                        "task_id": "independent-readiness", "as_of": NOW}

    def hint(self, installed, kind, context, state):
        from product_radar.source_coverage import source_readiness_hint
        return source_readiness_hint(installed, kind, context, state=state)

    def canonical_files(self):
        return {str(path.relative_to(self.memory.root)): path.read_bytes()
                for path in self.memory.root.rglob("*.json")
                if "direct_marketplace" not in path.parts}

    def test_installed_pair_uses_same_function_without_snapshot_or_mutation(self):
        state = self.memory.snapshot(); before = deepcopy(state); files = self.canonical_files()
        for kind in ("due_hint", "saved_hint"):
            base = self.installed["marketplace_" + kind]
            self.assertIs(self.installed["marketplace_" + kind + "_from_state"], base)
            expected = base(self.context)
            with patch.object(self.memory, "snapshot", side_effect=AssertionError("Nested canonical snapshot")):
                actual = self.hint(self.installed, kind, self.context, state)
            self.assertEqual(actual, expected)
        self.assertEqual(state, before)
        self.assertEqual(self.canonical_files(), files)
        self.assertEqual(self.source.calls, [])

    def test_replaced_one_argument_base_is_not_bypassed_by_old_pair(self):
        state = self.memory.snapshot(); installed = dict(self.installed); calls = []
        def replacement(context):
            calls.append(deepcopy(context)); return {"ready": False, "reason": "Custom source wait"}
        installed["marketplace_due_hint"] = replacement
        self.assertIsNot(installed["marketplace_due_hint_from_state"], replacement)
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("Unexpected canonical snapshot")):
            result = self.hint(installed, "due_hint", self.context, state)
        self.assertEqual(result, {"ready": False, "reason": "Custom source wait"})
        self.assertEqual(calls, [self.context])

    def test_existing_source_access_priority_overrides_legacy_marketplace_pair(self):
        state = self.memory.snapshot(); installed = dict(self.installed); calls = []
        def access(context):
            calls.append(context["task_id"]); return {"ready": False, "reason": "Access-specific wait"}
        installed["source_access_hint"] = access
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("Wrong lower-priority pair")):
            result = self.hint(installed, "due_hint", self.context, state)
        self.assertEqual(result["reason"], "Access-specific wait")
        self.assertEqual(calls, [self.context["task_id"]])

    def test_actual_company_pairs_preserve_one_argument_override(self):
        from product_radar.supplier_route_acquisition import create_supplier_handlers
        installed = create_supplier_handlers(self.memory, shared_acquisitions=True)["company-pages"]
        state = self.memory.snapshot(); before = deepcopy(state); files = self.canonical_files()
        context = {"route": "company-pages", "task_id": "company-review", "as_of": NOW,
                   "request": {"candidate_urls": {}}, "reconciling": False}
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("Company nested canonical snapshot")):
            for kind in ("due_hint", "saved_hint"):
                self.assertIs(installed["source_" + kind + "_from_state"], installed["source_" + kind])
                self.hint(installed, kind, context, state)
            def override(_context):
                raise SafetyError("RESEARCH_EVENTS_INVALID")
            installed["source_due_hint"] = override
            with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
                self.hint(installed, "due_hint", context, state)
        self.assertEqual(state, before)
        self.assertEqual(self.canonical_files(), files)
        self.assertEqual(self.source.calls, [])

    def test_discovery_propagates_corrupt_replaced_one_argument_base(self):
        from product_radar.meta_browser import meta_direct_source_handlers
        handlers = {route: dict(value) for route, value in self.source.handlers.items()}
        # Meta is the one discovery route (D4); its shared leaf's hint is replaced.
        handlers["meta-ads"] = dict(meta_direct_source_handlers(self.memory, shared_acquisitions=True,
            browser=lambda *_args, **_options: self.fail("No Meta page is read")))
        def corrupt(_context):
            raise SafetyError("RESEARCH_EVENTS_INVALID")
        handlers["meta-ads"]["source_due_hint"] = corrupt
        state = self.memory.snapshot(); before = deepcopy(state); files = self.canonical_files()
        with self.assertRaisesRegex(SafetyError, "RESEARCH_EVENTS_INVALID"):
            discovery_wave_readiness(self.memory, "independent", catalogue_fixture(), handlers=handlers,
                                     as_of=NOW, state=state)
        self.assertEqual(state, before)
        self.assertEqual(self.canonical_files(), files)
        self.assertEqual(self.source.calls, [])

    def test_actual_discovery_reuses_verified_state_without_writing(self):
        state = self.memory.snapshot(); before = deepcopy(state); files = self.canonical_files()
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("Discovery nested canonical snapshot")):
            result = discovery_wave_readiness(self.memory, "independent", catalogue_fixture(),
                handlers=self.source.handlers, as_of=NOW, state=state)
        self.assertTrue(result["new_wave_ready"])
        self.assertEqual(state, before)
        self.assertEqual(self.canonical_files(), files)
        self.assertEqual(self.source.calls, [])

    def test_late_original_file_changes_hint_without_canonical_event_change(self):
        request = build_marketplace_request("amazon-search", term="sink caddy")
        owner = claim_source_acquisition(self.memory, "amazon-search", request,
            owner_id=self.context["task_id"], as_of=NOW, artifact_roots=[self.source.source_root])
        state = self.memory.snapshot(); before = deepcopy(state); files = self.canonical_files()
        context = dict(self.context, acquisition_id=owner["acquisition_id"])
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("Late-file nested canonical snapshot")):
            first = self.hint(self.installed, "saved_hint", context, state)
            # A wake hint checks exact original-file arrival, not answer validity.
            # These synthetic bytes deliberately do not claim a parsed response.
            atomic_json(self.source.source_root / owner["acquisition_id"] / "native_result.json",
                        {"synthetic": "The original native child saved new bytes."})
            second = self.hint(self.installed, "saved_hint", context, state)
        self.assertNotEqual(first, second)
        self.assertEqual(first["acquisition_id"], second["acquisition_id"])
        self.assertTrue(any(row[0] == "native_result.json" for row in second["files"]))
        self.assertEqual(state, before)
        self.assertEqual(self.canonical_files(), files)
        self.assertEqual(self.source.calls, [])

    def test_next_cold_inspection_uses_new_canonical_refusal_not_cached_state(self):
        run = self.source.prepare("first-refusal", "amazon-search")
        first = self.memory.snapshot()
        initial = self.hint(self.installed, "due_hint", self.context, first)
        self.assertTrue(initial["ready"])
        self.source.execute(run)
        cold = ResearchMemory(self.memory.root, self.memory.journal, evidence_type="synthetic")
        current = cold.snapshot(); files = self.canonical_files(); before = deepcopy(current)
        with patch.object(self.memory, "snapshot", side_effect=AssertionError("Fresh inspection nested snapshot")):
            latest = self.hint(self.installed, "due_hint", self.context, current)
        self.assertFalse(latest["ready"])
        self.assertEqual(latest["signal"]["wait"]["phase"], "waiting")
        self.assertNotEqual(digest(first), digest(current))
        self.assertEqual(current, before)
        self.assertEqual(self.canonical_files(), files)
        self.assertEqual(len(self.source.calls), 1)

    def test_stale_ready_hint_cannot_bypass_actual_dispatch_fresh_refusal(self):
        first_run = self.source.prepare("refusing-owner", "amazon-search")
        next_run = self.source.prepare("healthy-before-refusal", "amazon-search", term="storage basket")
        state = self.memory.snapshot()
        task = next(row for row in state["query_tasks"].values()
                    if row.get("kind") == "all_source_task_v1" and row["run_id"] == next_run)
        context = source_access_context(state, task, as_of=NOW)
        self.assertTrue(self.hint(self.installed, "due_hint", context, state)["ready"])
        self.source.execute(first_run)
        calls = len(self.source.calls)
        with patch.object(self.memory, "snapshot", wraps=self.memory.snapshot) as snapshots:
            resolution = self.installed["resolve_marketplace"](context)
        self.assertGreater(snapshots.call_count, 0)
        self.assertEqual(resolution["action"], "wait")
        self.assertEqual(len(self.source.calls), calls)
        result = self.source.execute(next_run)
        self.assertEqual(result["handler_calls"], 0)
        self.assertEqual(len(self.source.calls), calls)

