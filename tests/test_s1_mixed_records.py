"""Mixed-record reader proof, using isolated saved synthetic research only.

These checks exercise existing consumers, rather than claiming that successful
record preparation is a source attempt, a market vote or a delivered report.
No provider, browser, live model, production store or cloud account is used.
"""

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from product_radar.brand_reviews import current_brand, prepare_brand_review
from product_radar.catalogue_coverage import catalogue_matrix
from product_radar.catalogue_expansion import effective_catalogue, run_expansion
from product_radar.functional_types import (
    accept_functional_definition, effective_functional_definition,
    prepare_example_memberships,
)
from product_radar.identity_corrections import apply_unallocated_identity_merge
from product_radar.models import SafetyError, digest
from product_radar.operational_backup_selection import select_essential_backup_files
from product_radar.product_queries import (
    current_query_profile, ensure_query_profiles, product_query_terms,
)
from product_radar.programme import prepare_programme, programme_progress
from product_radar.programme_summary import programme_cost_capacity_summary
from product_radar.query_dispatch import find_existing_task
from product_radar.report_selection import build_report_pool
from product_radar.report_source_facts import coverage_rows, route_dependencies
from product_radar.research_costs import collection_costs
from product_radar.research_batch import execute_plan
from product_radar.research_flow import candidate_summary, evidence_indexes, run_flow
from product_radar.research_memory import ResearchMemory
from product_radar.research_operations import (
    missing_market_checks, plan_research, save_source_check, sync_query_feedback,
)
from product_radar.research_records import (
    CATALOGUE_SCOPE_KIND, SCOPED_EVIDENCE_KIND,
    iter_catalogue_scopes, iter_example_memberships,
    iter_functional_definitions, iter_scoped_evidence,
)
from product_radar.research_scope import (
    accept_catalogue_scope, accept_scoped_evidence, validated_scoped_evidence,
)
from product_radar.source_coverage import prepare_source_tasks, source_coverage
from product_radar.storage import Journal
from product_radar.visual_followups import resolve_saved_visual_followups
from test_catalogue_expansion import Runner as ExpansionRunner, fixture as catalogue
from test_research_flow import FakeRunner, capture


NOW = "2026-10-06T10:00:00+00:00"
LEGACY_POLICY = "fit-13-v1"


class NoLaunchRunner:
    identity = "synthetic-only-v1"

    def __call__(self, *_args, **_kwargs):
        raise AssertionError("A saved mixed-record reader must not launch a model.")


class MixedRecordConsumerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="s1-mixed-records-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.memory = ResearchMemory(
            self.root / "data/research/captured", Journal(self.root / "finance"),
            evidence_type="synthetic",
        )
        # Real local import/Jobs/application contracts, with fictional A/B/E
        # answers. The historical policy is deliberate compatibility coverage.
        capture(self.memory)
        run_flow(self.memory, FakeRunner(), as_of=NOW, fit_policy_id=LEGACY_POLICY)
        state = self.memory.snapshot()
        self.concept_id = next(cid for cid, row in state["concepts"].items()
                               if row["description"]["label"] == "Sink caddy")
        self.concept_ids = sorted(state["concepts"])
        self.base = catalogue()

    def add_type_overlays(self):
        state = self.memory.snapshot()
        before = effective_functional_definition(state, self.concept_id)
        semantics = {row["semantic_id"] for row in state["associations"].values()
                     if row.get("concept_id") == self.concept_id
                     and row.get("status") == "resolved"}
        refs = [ref for ref, row in state["observations"].items()
                if row["semantic_id"] in semantics]
        definition = {
            "report_title": "Sink caddy", "primary_use": "Store sink essentials",
            "mechanism": "manual", "use_context": [],
            "compatibility_constraints": [], "included_distinctions": ["draining"],
            "excluded_distinctions": [],
        }
        accept_functional_definition(
            self.memory, self.concept_id, definition, basis_refs=refs,
            reviewer="fixture_owner", reviewed_at=NOW,
            expected_meaning_hash=before["meaning_hash"],
            example_decisions={child_id: {
                "decision": "same", "reason": "The saved functional kind is unchanged in this fixture.",
            } for child_id, row in state["associations"].items()
                if row.get("concept_id") == self.concept_id and row.get("status") == "resolved"},
        )
        membership = prepare_example_memberships(self.memory, self.concept_id, as_of=NOW)
        # S1 initially had dispatch-only readers. These contracts now also feed
        # real semantic consumers, so incomplete typed dictionaries are corrupt
        # records, not harmless overlays. Use the actual writers and an explicit
        # broader-context review: neither its preparation nor its acceptance is
        # a new source attempt or a narrow-type market vote. The new draining
        # meaning deliberately cannot inherit the legacy fit/market judgments.
        state = self.memory.snapshot()
        references = [{"table": "observations", "id": ref,
                       "hash": digest(state["observations"][ref])} for ref in refs]
        accept_catalogue_scope(self.memory, self.base, self.concept_id, "caddy",
            relationship="narrower", references=references,
            reason="The draining functional type is narrower than this saved catalogue heading.",
            reviewer="fixture_owner", as_of=NOW,
            expected_meaning_hash=membership["meaning_hash"])
        accept_scoped_evidence(self.memory, self.concept_id,
            route="meta-ads", scope="broader_context", relationship="broader_context",
            support="insufficient", references=references, usable_fields=[],
            limitations=["Broader context only; no narrow-type market support is asserted."],
            reason="These original ads have not been admitted as narrow-type market support.",
            reviewer="fixture_owner", as_of=NOW,
            expected_meaning_hash=membership["meaning_hash"])
        # An unknown explicit kind is not promoted to provider availability just
        # because it happens to contain the old source-status field names.
        self.memory.put("source_checks", "reader-only-unknown-overlay", {
            "kind": "future-overlay-v999", "concept_id": self.concept_id,
            "source": "trends", "route": "browser", "at": NOW,
            "state": "usable", "reason": "This is not a source-status check.",
            "reference": "reader-only-unknown-overlay",
        })
        return membership

    def add_v2_questions(self):
        from product_radar.research_questions import (
            catalogue_direction_subject, prepare_research_questions,
        )

        state = self.memory.snapshot()
        concept = {"kind": "concept", "concept_id": self.concept_id,
            "definition_hash": effective_functional_definition(state, self.concept_id)["meaning_hash"]}
        direction = catalogue_direction_subject(state, "caddy", catalogue=self.base)
        return prepare_research_questions(self.memory, [
            {"subject": concept, "route": "amazon-search", "purpose": "validation",
             "request": {"query": "sink caddy", "country": "IN"}},
            {"subject": direction, "route": "meta-ads", "purpose": "discovery",
             "request": {"query": "sink caddy", "country": "IN"}},
        ], as_of=NOW, catalogue=self.base)

    def test_flow_indexes_keep_legacy_peer_and_do_not_inherit_narrowed_type_judgments(self):
        before = self.memory.snapshot()
        indexes = evidence_indexes(before)
        summary = candidate_summary(self.memory, as_of=NOW)
        membership = self.add_type_overlays()
        self.assertEqual(len(membership["membership_ids"]), 2)
        after = self.memory.snapshot()
        self.assertEqual(after["associations"], before["associations"])
        self.assertEqual(evidence_indexes(after), indexes)
        current = candidate_summary(self.memory, as_of=NOW)
        original_products = {row["id"]: row for row in summary["products"]}
        current_products = {row["id"]: row for row in current["products"]}
        for cid in self.concept_ids:
            if cid != self.concept_id:
                self.assertEqual(current_products[cid], original_products[cid])
        narrowed = current_products[self.concept_id]
        self.assertIsNone(narrowed["checklist_meets"])
        self.assertIsNone(narrowed["fit_policy_id"])
        self.assertEqual(narrowed["criteria"], [])
        self.assertEqual(narrowed["market"]["available_families"], 0)
        self.assertEqual(narrowed["market"]["positive_families"], 0)
        self.assertIsNone(narrowed["market_scope_problem"])
        self.assertEqual(after["assessments"], before["assessments"])
        self.assertEqual(after["market_reviews"], before["market_reviews"])
        for field in ("captures", "observations", "described_items"):
            self.assertEqual(current[field], summary[field])
        jobs = deepcopy(after["jobs"])
        run_flow(self.memory, NoLaunchRunner(), as_of=NOW, fit_policy_id=LEGACY_POLICY)
        self.assertEqual(self.memory.snapshot()["jobs"], jobs)

    def test_positive_overlay_readers_keep_kinds_and_subjects_separate(self):
        self.add_type_overlays()
        state = self.memory.snapshot()
        readers = (
            (iter_functional_definitions, 1), (iter_example_memberships, 2),
            (iter_catalogue_scopes, 1), (iter_scoped_evidence, 1),
        )
        for reader, count in readers:
            with self.subTest(reader=reader.__name__):
                records = list(reader(state, concept_id=self.concept_id))
                self.assertEqual(len(records), count)
                self.assertTrue(all(ref != "reader-only-unknown-overlay" for ref, _ in records))
                self.assertEqual(list(reader(state, concept_id="different-concept")), [])
        self.assertEqual(self.memory.snapshot(), state)

    def test_catalogue_growth_replays_original_answers_without_new_calls(self):
        runner = ExpansionRunner()
        original = run_expansion(self.memory, self.base, runner,
            concept_ids=self.concept_ids, as_of=NOW)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual({row["outcome"] for row in original["records"]}, {"merged", "accepted"})
        effective = effective_catalogue(self.base, self.memory.snapshot())
        self.add_type_overlays()
        replay = run_expansion(self.memory, self.base, NoLaunchRunner(),
            concept_ids=self.concept_ids, as_of=NOW)
        self.assertEqual(replay, original)
        self.assertEqual(effective_catalogue(self.base, self.memory.snapshot()), effective)
        self.assertEqual(len(self.base["families"]), 1)

    def test_cached_profiles_keep_legacy_peers_and_new_meaning_requires_its_own_contract(self):
        ensure_query_profiles(self.memory, NoLaunchRunner(), concept_ids=self.concept_ids,
                              catalogue=self.base, as_of=NOW)
        before = self.memory.snapshot()
        profiles = {cid: current_query_profile(before, cid) for cid in self.concept_ids}
        self.assertTrue(all(profiles.values()))
        terms = {source: product_query_terms(self.memory, self.concept_id,
                    source=source, catalogue=self.base)
                 for source in ("meta", "marketplace", "trends", "supplier", "quickcommerce")}
        self.add_type_overlays()
        state = self.memory.snapshot()
        for cid in self.concept_ids:
            if cid != self.concept_id:
                self.assertEqual(current_query_profile(state, cid), profiles[cid])
        # An old retrieval family is not silently promoted to the newly
        # accepted functional contract, even when broad legacy fields agree.
        self.assertIsNone(current_query_profile(state, self.concept_id))
        for ref, row in before["source_checks"].items():
            self.assertEqual(state["source_checks"][ref], row)
        from test_functional_queries import Runner as FunctionalQueryRunner
        runner = FunctionalQueryRunner()
        prepared = ensure_query_profiles(self.memory, runner, concept_ids=[self.concept_id],
                                         catalogue=self.base, as_of=NOW)
        self.assertEqual(prepared["pending"], [])
        self.assertEqual(len(runner.calls), 1)
        profile = current_query_profile(self.memory.snapshot(), self.concept_id)
        self.assertEqual(profile["meaning_hash"], effective_functional_definition(state, self.concept_id)["meaning_hash"])
        from product_radar.functional_types import DEFINITION_FIELDS
        view = effective_functional_definition(self.memory.snapshot(), self.concept_id)
        definition = {field: deepcopy(view[field]) for field in DEFINITION_FIELDS}
        definition["report_title"] = "A tidy sink"
        record = self.memory.snapshot()["source_checks"][view["definition_id"]]
        accept_functional_definition(self.memory, self.concept_id, definition,
            basis_refs=list(record["basis_refs"]), reviewer="fixture_owner",
            reviewed_at="2026-10-06T10:00:00+00:00", expected_meaning_hash=view["meaning_hash"])
        self.assertEqual(current_query_profile(self.memory.snapshot(), self.concept_id), profile)
        frozen = self.memory.snapshot()
        ensure_query_profiles(self.memory, NoLaunchRunner(), concept_ids=self.concept_ids,
                              catalogue=self.base, as_of=NOW)
        self.assertEqual(self.memory.snapshot(), frozen)
        self.assertEqual(len(runner.calls), 1)

    def test_brand_scope_and_visual_refusal_stay_on_original_children(self):
        before = self.memory.snapshot()
        association_id = next(ref for ref, row in before["associations"].items()
                              if row.get("concept_id") == self.concept_id)
        semantics = {row["semantic_id"] for row in before["associations"].values()
                     if row.get("concept_id") == self.concept_id}
        refs = [ref for ref, row in before["observations"].items()
                if row["semantic_id"] in semantics]
        options = {"verdict": "generic_or_small", "reason": "Fictional source-bound owner review.",
                   "evidence_ids": refs, "reviewer": "fixture_owner", "reviewed_at": NOW}
        review = prepare_brand_review(before, "concept", self.concept_id, **options)
        self.add_type_overlays()
        state = self.memory.snapshot()
        self.assertEqual(prepare_brand_review(state, "concept", self.concept_id, **options), review)
        self.assertEqual(current_brand(state, "concept", self.concept_id,
            state["concepts"][self.concept_id]["description"]), "generic_or_small")
        result = resolve_saved_visual_followups(self.memory,
            [{"child_id": association_id, "images": []}], NoLaunchRunner(), as_of=NOW)
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["collector_requests"], 0)
        self.assertEqual(result["results"][0]["reason"], "VISUAL_DEFERRED_CHILD_REQUIRED")
        self.assertEqual(self.memory.snapshot(), state)

    def test_new_overlays_do_not_bypass_a_real_issue_merge_hold(self):
        self.add_type_overlays()
        self.memory.put("issue_reservations", "protected-issue", {
            "state": "reserved", "products": [{"concept_id": self.concept_id}],
        })
        state = self.memory.snapshot()
        request = {
            "survivor_id": self.concept_id,
            "concept_hashes": {cid: digest(state["concepts"][cid]) for cid in self.concept_ids},
            "association_hashes": {ref: digest(row) for ref, row in state["associations"].items()
                                   if row.get("concept_id") in self.concept_ids},
            "evidence_ids": {cid: [ref for ref, obs in state["observations"].items()
                if obs["semantic_id"] in {row["semantic_id"] for row in state["associations"].values()
                    if row.get("concept_id") == cid}] for cid in self.concept_ids},
            "author": "fixture_owner", "reason": "Synthetic request must not bypass the real report hold.",
            "reviewed_at": NOW,
        }
        with self.assertRaisesRegex(SafetyError, "IDENTITY_MERGE_RELEASE_REVIEW_REQUIRED"):
            apply_unallocated_identity_merge(self.memory, request)
        self.assertEqual(self.memory.snapshot(), state)

    def test_report_pool_and_source_facts_do_not_invent_votes_or_attempts(self):
        before = self.memory.snapshot()
        pool = build_report_pool(self.memory, as_of=NOW, fit_policy_id=LEGACY_POLICY)
        self.assertEqual(len(pool["facts_by_concept"]), 2)
        coverage = coverage_rows(self.memory, before, self.concept_ids, NOW)
        self.add_type_overlays()
        after = self.memory.snapshot()
        updated = build_report_pool(self.memory, as_of=NOW, fit_policy_id=LEGACY_POLICY)
        for cid in self.concept_ids:
            if cid != self.concept_id:
                self.assertEqual(updated["facts_by_concept"][cid], pool["facts_by_concept"][cid])
        self.assertNotIn(self.concept_id, {row["concept_id"] for row in updated["eligible"]})
        decision = next(row for row in updated["decisions"] if row["concept_id"] == self.concept_id)
        self.assertIn("checklist_review_required", decision["codes"])
        narrowed = updated["facts_by_concept"][self.concept_id]
        self.assertEqual(narrowed["criteria"], [])
        self.assertFalse(any(row.get("diagnostic_code") == "FIT_CRITERION_COVERAGE_INVALID"
                             for row in updated["decisions"]))
        self.assertEqual(after["assessments"], before["assessments"])
        self.assertEqual(after["jobs"], before["jobs"])
        current_coverage = coverage_rows(self.memory, after, self.concept_ids, NOW)
        for cid in self.concept_ids:
            if cid != self.concept_id:
                self.assertEqual(current_coverage[cid], coverage[cid])
        self.assertEqual(route_dependencies(after, self.concept_id), route_dependencies(before, self.concept_id))
        self.assertEqual(after["captures"], before["captures"])
        self.assertEqual(after["observations"], before["observations"])

    def test_source_status_and_research_plan_ignore_definition_fields(self):
        save_source_check(self.memory, source="trends", route="browser", at=NOW,
            state="blocked", reason="Synthetic existing route check.", reference="fixture-route-status")
        summary = candidate_summary(self.memory, as_of=NOW)
        before = self.memory.snapshot()
        gaps = missing_market_checks(summary, as_of=NOW, source_checks=before["source_checks"].values())
        self.add_type_overlays()
        mixed = self.memory.snapshot()
        self.assertEqual(missing_market_checks(summary, as_of=NOW,
            source_checks=mixed["source_checks"].values()), gaps)
        plan = plan_research(self.memory, self.base, as_of=NOW, slots=1, summary=summary)
        self.assertEqual(len(plan["actions"]), 1)
        self.assertEqual(len(plan["source_checks"]), 1)
        self.assertEqual(plan["source_checks"][0]["reason"], "Synthetic existing route check.")
        self.assertFalse(plan["collector_dispatched"])
        self.assertEqual(self.memory.snapshot()["jobs"], mixed["jobs"])
        self.assertFalse(self.memory.journal.path.exists())

    def test_v2_questions_never_become_actor_reuse_feedback_or_paid_coverage(self):
        self.add_type_overlays()
        questions = self.add_v2_questions()
        self.assertEqual(len(questions["question_ids"]), 2)
        state = self.memory.snapshot()
        matrix = catalogue_matrix(self.base, state, financial_attempts={})
        self.assertEqual(matrix["totals"]["meta_discovery_tried"], 0)
        self.assertEqual(matrix["totals"]["meta_discovery_not_dispatched"], 1)
        self.assertTrue(all(row["state"] == "not_tried" for row in matrix["rows"]))
        plan = plan_research(self.memory, self.base, as_of=NOW, slots=1)
        action = plan["actions"][0]
        self.assertIsNone(find_existing_task(self.memory, action, as_of=NOW))
        self.assertEqual(sync_query_feedback(self.memory, as_of=NOW), [])
        self.assertEqual(self.memory.snapshot()["query_tasks"], state["query_tasks"])
        self.assertEqual(self.memory.snapshot()["query_outcomes"], {})
        with self.memory.journal.locked():
            self.assertEqual(collection_costs(self.memory.journal)["physical_acquisitions"], 0)
        self.assertFalse(self.memory.journal.path.exists())

    def test_real_legacy_paid_preparation_remains_reusable_among_v2_questions(self):
        from product_radar.models import prepared_request
        from product_radar.research_operations import bind_prepared_action
        import test_meta_live_trial as meta_fixture

        plan = plan_research(self.memory, self.base, as_of=NOW, slots=1)
        action = plan["actions"][0]
        config = deepcopy(meta_fixture.CONFIG)
        config.update(term=action["phrase"], purpose="discovery")
        prepared = prepared_request(meta_fixture.cli.prepare(config, "mixed-prepared-fixture")["request"])
        binding = bind_prepared_action(self.memory, plan["id"], action["action_id"], prepared)
        self.add_type_overlays()
        self.add_v2_questions()
        current = self.memory.snapshot()
        misleading = deepcopy(current["query_tasks"][binding["id"]])
        misleading["kind"] = "future-explicit-task-v999"
        self.memory.put("query_tasks", "unknown-typed-task", misleading)
        self.assertEqual(find_existing_task(self.memory, action, as_of=NOW)["task_id"], binding["id"])
        self.assertEqual(sync_query_feedback(self.memory, as_of=NOW), [])
        with self.memory.journal.locked():
            self.assertEqual(collection_costs(self.memory.journal)["physical_acquisitions"], 0)
        matrix = catalogue_matrix(self.base, self.memory.snapshot(), financial_attempts={})
        self.assertEqual(matrix["rows"][0]["prepared_task_ids"], [binding["id"]])
        self.assertEqual(matrix["rows"][0]["state"], "prepared_not_dispatched")
        self.assertFalse(self.memory.journal.path.exists())

    def test_paid_execution_checks_its_own_preflight_and_never_dispatches_v2_questions(self):
        from product_radar.models import prepared_request
        import test_meta_live_trial as meta_fixture

        self.add_type_overlays()
        self.add_v2_questions()
        original_questions = self.memory.snapshot()["query_tasks"]
        plan = plan_research(self.memory, self.base, as_of=NOW, slots=1)
        checks = []

        class DeniedSyntheticController:
            def preflight(self, attempt_id):
                checks.append(attempt_id)
                return {"ready_for_live": False}

            def run(self, *_args, **_kwargs):
                raise AssertionError("Failed preflight must never reach a paid provider.")

        def prepare(_plan, action):
            config = deepcopy(meta_fixture.CONFIG)
            config.update(term=action["phrase"], purpose="discovery")
            return prepared_request(meta_fixture.cli.prepare(config, "mixed-denied-fixture")["request"])

        result = execute_plan(self.memory, plan["id"], prepare_action=prepare,
            controller_factory=DeniedSyntheticController, runner=NoLaunchRunner())
        self.assertEqual(result["blocked_actions"], 1)
        self.assertEqual(result["actions"][0]["reason"], "BATCH_PREFLIGHT_NOT_READY")
        self.assertEqual(result["acquisition_ids"], [])
        self.assertEqual(len(checks), 1)
        for ref, question in original_questions.items():
            self.assertEqual(self.memory.snapshot()["query_tasks"][ref], question)
        replay = execute_plan(self.memory, plan["id"], prepare_action=prepare,
            controller_factory=DeniedSyntheticController, runner=NoLaunchRunner())
        self.assertTrue(replay["replay"])
        self.assertEqual(len(checks), 1)
        self.assertEqual(replay["acquisition_ids"], [])
        self.assertFalse(self.memory.journal.path.exists())

    def test_programme_coverage_and_costs_keep_preparation_separate_from_attempts(self):
        ensure_query_profiles(self.memory, NoLaunchRunner(), concept_ids=self.concept_ids,
                              catalogue=self.base, as_of=NOW)
        prepare_source_tasks(self.memory, [{"route": "amazon-search",
            "request": {"query": "sink caddy", "country": "IN"},
            "concept_ids": [self.concept_id]}], run_id="legacy-source-run", as_of=NOW)
        programme = prepare_programme(self.memory, "mixed-programme",
            source_run_id="legacy-source-run", concept_ids=self.concept_ids, as_of=NOW)
        state = self.memory.snapshot()
        progress = programme_progress(self.memory, programme["programme_id"], handlers={}, as_of=NOW)
        coverage = source_coverage(self.memory, handlers={}, concept_ids=self.concept_ids,
                                  as_of=NOW, state=state)
        pool = build_report_pool(self.memory, as_of=NOW, fit_policy_id=LEGACY_POLICY)
        totals = programme_cost_capacity_summary(programme, state=state, financial_state=({}, {}),
            catalogue=self.base, report_pool=pool, as_of=NOW)
        self.add_type_overlays()
        self.add_v2_questions()
        mixed = self.memory.snapshot()
        current_progress = programme_progress(self.memory, programme["programme_id"], handlers={}, as_of=NOW)
        current_coverage = source_coverage(self.memory, handlers={}, concept_ids=self.concept_ids,
            as_of=NOW, state=mixed)
        for field in ("products", "fit_products", "source_work", "product_route_questions",
                      "actually_attempted_product_routes", "saved_usable_photo_records"):
            self.assertEqual(current_progress[field], progress[field])
        self.assertEqual(current_progress["current_query_profiles"], progress["current_query_profiles"] - 1)
        self.assertEqual(current_coverage["actually_attempted_product_routes"], coverage["actually_attempted_product_routes"])
        peer_rows = lambda grid: [row for row in grid["rows"] if row["concept_id"] != self.concept_id]
        self.assertEqual(peer_rows(current_coverage), peer_rows(coverage))
        updated = programme_cost_capacity_summary(programme, state=mixed, financial_state=({}, {}),
            catalogue=self.base, report_pool=pool, as_of=NOW)
        for name in ("research", "money", "report_supply"):
            self.assertEqual(updated[name], totals[name])
        self.assertEqual(updated["catalogue"]["totals"], totals["catalogue"]["totals"])
        self.assertEqual(updated["research"]["prepared_source_queries"], 1)
        self.assertEqual(updated["research"]["shared_controller_acquisition_attempts"], 0)
        self.assertEqual(updated["money"]["ledger_physical_acquisitions"], 0)
        self.assertEqual(updated["money"]["ledger_known_incurred_usd"], "0")
        self.assertFalse(updated["client_publication_attempted"])
        self.assertEqual(self.memory.snapshot(), mixed)
        self.assertFalse(self.memory.journal.path.exists())

    def test_corrupt_scoped_overlay_is_not_silently_ignored_or_promoted(self):
        self.add_type_overlays()
        prepare_source_tasks(self.memory, [{"route": "amazon-search",
            "request": {"query": "sink caddy", "country": "IN"},
            "concept_ids": [self.concept_id]}], run_id="corruption-source-run", as_of=NOW)
        programme = prepare_programme(self.memory, "corruption-programme",
            source_run_id="corruption-source-run", concept_ids=self.concept_ids, as_of=NOW)
        state = self.memory.snapshot()
        record_id, record = next(iter_scoped_evidence(state, concept_id=self.concept_id))
        corrupted = deepcopy(state)
        corrupted["source_checks"][record_id]["support"] = "positive"
        with self.assertRaisesRegex(SafetyError, "RESEARCH_SCOPE_RECORD_HASH_MISMATCH"):
            validated_scoped_evidence(corrupted, self.concept_id, as_of=NOW)
        with self.assertRaisesRegex(SafetyError, "RESEARCH_SCOPE_RECORD_HASH_MISMATCH"):
            programme_progress(self.memory, programme["programme_id"],
                handlers={}, as_of=NOW, state=corrupted)
        self.assertEqual(self.memory.snapshot(), state)
        self.assertEqual(state["source_checks"][record_id], record)
        self.assertFalse(self.memory.journal.path.exists())

    def test_original_job_bytes_are_selected_for_backup_not_fabricated_overlay_paths(self):
        before = self.memory.snapshot()
        baseline = select_essential_backup_files(before, ({}, {}), root=self.root)
        self.assertTrue(baseline["files"])
        self.add_type_overlays()
        self.add_v2_questions()
        mixed = self.memory.snapshot()
        selected = select_essential_backup_files(mixed, ({}, {}), root=self.root)
        compare = lambda rows: {row["relative_path"]: (row["expected_sha256"], row["bytes"])
                                for row in rows}
        self.assertEqual(compare(selected["files"]), compare(baseline["files"]))
        self.assertEqual(selected["unsafe"], baseline["unsafe"])
        self.assertEqual(self.memory.snapshot(), mixed)
        self.assertFalse(self.memory.journal.path.exists())


if __name__ == "__main__":
    unittest.main()
