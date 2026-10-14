"""Directional real-record history joins; no live publication or source calls."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

import test_functional_lineage as lineage_fixture
from test_report_selection import NOW
from test_functional_source_market import functional_pool_fixture
from test_functional_queries import DEFINITION, Runner as QueryRunner
from test_functional_fit import FunctionalFitRunner
from product_radar.crowding import refresh_crowding
from product_radar.fit_description import FUNCTIONAL_CONTRACT
from product_radar.fit_review import prepare_practical_fit_reviews, run_fit_reviews
from product_radar.functional_lineage import accept_functional_lineage, prepare_lineage_input
from product_radar.functional_types import accept_functional_definition, effective_functional_definition, prepare_example_memberships
from product_radar.issue_allocation import allocate_issues
from product_radar.material_updates import publication_baseline
from product_radar.models import digest
from product_radar.product_queries import ensure_query_profiles
from product_radar.report_examples import RECOMMENDATION_CONTRACT
from product_radar.report_selection import _seen, build_report_pool
from product_radar.research_memory import ResearchMemory, change
from product_radar.research_scope import accept_scoped_evidence
from product_radar.service import policy_for_fit
from product_radar.storage import Journal
import product_radar.research_segments as segments


class SegmentedLineageHistoryTests(unittest.TestCase):
    def setUp(self):
        self.saved = lineage_fixture.FunctionalLineageTests()
        self.saved.setUp(); self.addCleanup(self.saved.doCleanups)
        self.memory = self.saved.memory

    def restart(self):
        return ResearchMemory(self.memory.root, self.memory.journal, evidence_type="synthetic")

    def test_existing_parent_publication_reaches_child_after_migration_without_rewriting_ids(self):
        self.saved.expose("old-parent", "bear")
        self.memory.migrate_segments()
        originals = {p.name: p.read_bytes() for p in (self.memory.root / "events").glob("*.json")}
        self.assertEqual(self.memory.novelty_page("bird")["total_rows"], 0)
        self.saved.accept()
        page = self.restart().novelty_page("bird")
        self.assertEqual(page["total_rows"], 1)
        self.assertEqual(page["records"][0], ("old-parent", self.memory.snapshot()["published_history"]["old-parent"]))
        self.assertEqual(page["canonical_id"], "bird")
        self.assertEqual(self.memory.novelty_page("bird", client_id="another_client")["total_rows"], 0)
        for name, data in originals.items(): self.assertEqual((self.memory.root / "events" / name).read_bytes(), data)
        self.assertEqual(self.memory.novelty_page("bird", section="aliases")["total_rows"], 1)

    def test_child_publication_does_not_reach_parent_or_sibling(self):
        self.saved.accept(); self.saved.accept("bear", "cat")
        self.saved.expose("old-child", "bird"); self.memory.migrate_segments()
        self.assertEqual(self.restart().novelty_page("bird")["total_rows"], 1)
        self.assertEqual(self.memory.novelty_page("bear")["total_rows"], 0)
        self.assertEqual(self.memory.novelty_page("cat")["total_rows"], 0)
        self.assertEqual({rid for rid, _ in self.memory.novelty_page("bird", section="aliases")["records"]}, {"bird"})

    def test_child_hold_overlaps_parent_but_not_sibling_and_retains_actual_held_row(self):
        self.saved.accept(); self.saved.accept("bear", "cat")
        self.saved.hold("child-hold", "bird"); self.memory.migrate_segments()
        for cid in ("bear", "bird"):
            page = self.restart().novelty_page(cid, section="holds")
            self.assertEqual(page["total_rows"], 1)
            self.assertEqual(page["records"][0][1]["products"][0]["concept_id"], "bird")
        self.assertEqual(self.memory.novelty_page("cat", section="holds")["total_rows"], 0)

    def test_parent_hold_reaches_both_children_without_equating_their_alias_pages(self):
        self.saved.accept(); self.saved.accept("bear", "cat")
        self.saved.hold("parent-hold", "bear"); self.memory.migrate_segments()
        for cid in ("bear", "bird", "cat"):
            self.assertEqual(self.memory.novelty_page(cid, section="holds")["total_rows"], 1)
        self.assertEqual(self.memory.novelty_page("bird", section="aliases")["records"][0][0], "bird")
        self.assertEqual(self.memory.novelty_page("cat", section="aliases")["records"][0][0], "cat")

    def test_lost_derived_checkpoint_falls_back_to_same_directional_original_history(self):
        self.saved.accept(); self.saved.expose("old-parent", "bear"); self.memory.migrate_segments()
        expected = self.memory.novelty_page("bird")["records"]
        pointer = segments.load_json(self.memory.root / segments.POINTER)
        segments._path(self.memory, pointer["checkpoint"]).unlink()
        reader = self.restart()
        self.assertEqual(reader.novelty_page("bird")["records"], expected)
        self.assertIsNotNone(reader.last_checkpoint_diagnostic)

    def test_all_two_thousand_inherited_publications_remain_bounded_and_paginated(self):
        self.saved.accept()
        self.memory.commit("two-thousand-parent-publications", [change("published_history", "old-" + f"{n:04d}",
            {"concept_id": "bear", "issue_id": "issue-" + str(n), "client_id": "default", "status": "published",
             "available_at": "2001-01-01T00:00:00+00:00"}) for n in range(2000)])
        self.memory.migrate_segments()
        reader = self.restart(); cursor, records, pages = None, [], 0
        while True:
            page = reader.novelty_page("bird", limit=128, cursor=cursor)
            self.assertLessEqual(len(page["records"]), 128)
            self.assertEqual(page["total_rows"], 2000)
            records.extend(page["records"]); pages += 1
            cursor = page["next_cursor"]
            if cursor is None: break
        self.assertEqual(pages, 16)
        self.assertEqual(len({rid for rid, _ in records}), 2000)
        self.assertEqual({row["concept_id"] for _, row in records}, {"bear"})
        self.assertEqual(reader.novelty_page("cat")["total_rows"], 0)


class ReportLineageHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.memory, ids = functional_pool_fixture(root, count=3, at=NOW)
        self.parent, self.child, self.sibling = ids
        for n, cid in enumerate(ids):
            state = self.memory.snapshot()
            children = {child: row for child, row in state["associations"].items() if row["concept_id"] == cid}
            semantics = {row["semantic_id"] for row in children.values()}
            refs = sorted(rid for rid, row in state["observations"].items() if row["semantic_id"] in semantics)
            old_scopes = [row for row in state["source_checks"].values()
                          if row.get("kind") == "scoped-evidence-v1" and row.get("concept_id") == cid]
            definition = dict(DEFINITION, compatibility_constraints=[] if n == 0 else
                              ["For narrow sink rims" if n == 1 else "For metal sink rims"])
            accept_functional_definition(self.memory, cid, definition, basis_refs=refs,
                reviewer="synthetic_operator", reviewed_at=NOW,
                expected_meaning_hash=effective_functional_definition(state, cid)["meaning_hash"],
                example_decisions={child: {"decision": "same", "reason": "Explicit fictional narrowed example."} for child in children})
            prepare_example_memberships(self.memory, cid, as_of=NOW)
            for row in old_scopes:
                accept_scoped_evidence(self.memory, cid, route=row["route"], scope=row["scope"],
                    relationship=row["relationship"], support=row["support"], references=row["references"],
                    usable_fields=[{field: value[field] for field in ("reference", "path")}
                                   for value in row["usable_fields"]], limitations=row["limitations"],
                    reason="Explicit synthetic review for the revised narrow meaning.", reviewer="synthetic_operator",
                    as_of=NOW, expected_meaning_hash=effective_functional_definition(self.memory.snapshot(), cid)["meaning_hash"])
        ensure_query_profiles(self.memory, QueryRunner(), concept_ids=ids, as_of=NOW)
        prepared = prepare_practical_fit_reviews(self.memory, concept_ids=ids, as_of=NOW, contract_version=FUNCTIONAL_CONTRACT)
        reviewed = run_fit_reviews(self.memory, FunctionalFitRunner(), [row["review_id"] for row in prepared])
        self.assertTrue(all(row["status"] == "accepted" for row in reviewed))
        state = self.memory.snapshot()
        refs = {cid: sorted(rid for rid, row in state["observations"].items() if row.get("semantic_id") in
            {a["semantic_id"] for a in state["associations"].values() if a.get("concept_id") == cid}) for cid in ids}
        for child in (self.child, self.sibling):
            prepared = prepare_lineage_input(state, self.parent, child,
                grounding_refs={cid: refs[cid] for cid in (self.parent, child)}, reviewed_at=NOW)
            accept_functional_lineage(self.memory, prepared, reviewer="synthetic_operator",
                reason="Explicit fictional narrowing with the same use and mechanism.",
                distinctions=["The child's sink compatibility is narrower."])
        # Revised meanings need their own free crowding answer (W6), as the stage would.
        refresh_crowding(self.memory, ids, as_of=NOW)

    def pool(self):
        return build_report_pool(self.memory, as_of=NOW, recommendation_contract=RECOMMENDATION_CONTRACT)

    def expose(self, cid, client="default"):
        self.memory.put("published_history", "old-" + cid, {"concept_id": cid, "issue_id": "old-" + cid,
            "status": "published", "client_id": client})

    def test_report_pool_parent_exposure_prevents_child_new_without_transferring_facts(self):
        before = self.pool()
        self.assertEqual(len(before["eligible"]), 3)
        self.expose(self.parent)
        after = self.pool()
        self.assertEqual(_seen(self.memory.snapshot()), {self.parent, self.child, self.sibling})
        self.assertEqual(after["eligible"], [])
        children = [row for row in after["decisions"] if row["concept_id"] != self.parent]
        self.assertTrue(all(row["useful_update_decision"] == "baseline_unavailable" for row in children))
        for cid in (self.parent, self.child, self.sibling):
            self.assertEqual(before["facts_by_concept"][cid]["source_refs"], after["facts_by_concept"][cid]["source_refs"])
        self.assertEqual(_seen(self.memory.snapshot(), "another_client"), set())

    def test_report_pool_child_exposure_keeps_parent_and_sibling_new(self):
        self.expose(self.child)
        pool = self.pool()
        self.assertEqual(_seen(self.memory.snapshot()), {self.child})
        self.assertEqual({row["concept_id"] for row in pool["eligible"]}, {self.parent, self.sibling})
        self.assertTrue(all(row["status"] == "New" for row in pool["eligible"]))

    def test_real_parent_comparison_baseline_does_not_become_a_child_supplier_baseline(self):
        parent = next(row for row in self.pool()["eligible"] if row["concept_id"] == self.parent)
        issue = allocate_issues([parent], ["2026-10-07"], {}, {}, policy=policy_for_fit("fit-10-v1"),
                                identity_state=self.memory.snapshot())["issues"][0]
        # Simulated original publication proof, never a real publication call.
        receipt = {"verified": True, "authority": "release_index", "issue_id": issue["issue_id"],
            "revision": issue["revision"], "selection_hash": issue["selection_hash"], "package_hash": "1" * 64,
            "facts_hash": "2" * 64}
        baseline = publication_baseline(issue, issue["products"][0], receipt)
        self.memory.put("published_history", "actual-parent-baseline", {"concept_id": self.parent,
            "issue_id": issue["issue_id"], "revision": issue["revision"], "client_id": "default",
            "status": "published", "available_at": NOW, "activation_receipt": receipt, "publication_baseline": baseline})
        old = deepcopy(self.memory.snapshot()["published_history"])
        pool = self.pool()
        child = next(row for row in pool["decisions"] if row["concept_id"] == self.child)
        self.assertEqual(child["useful_update_decision"], "baseline_unavailable")
        self.assertEqual(self.memory.snapshot()["published_history"], old)
        self.assertFalse(pool["needed_material_update_work"])


if __name__ == "__main__": unittest.main()
