"""Actual saved narrowing records joined to allocation; no external calls."""

from copy import deepcopy
import unittest

import test_functional_lineage as lineage_fixture
from test_issue_allocation import DATES, products
from product_radar.functional_aliases import alias_projection, selected_alias_binding
from product_radar.issue_allocation import (_fingerprint, _held, _published, allocate_issues,
    allocation_snapshot_hash, reserve_issue, selection_fingerprint)
from product_radar.models import SafetyError


class AllocationLineageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = lineage_fixture.FunctionalLineageTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.memory = self.fixture.memory
        self.fixture.accept(); self.fixture.accept("bear", "cat")

    def projection(self):
        return alias_projection(self.memory.snapshot())

    def candidates(self):
        values = products(14)
        for index, cid in enumerate(("bear", "bird", "cat")):
            values[index]["concept_id"] = cid
        # Allocation's supplied extra fictional candidates are actual saved
        # identities too; no unresolved identities are introduced by this test.
        template = self.memory.snapshot()["concepts"]["other"]
        for row in values[3:]:
            self.memory.put("concepts", row["concept_id"], dict(deepcopy(template), concept_id=row["concept_id"]))
        return values

    def test_parent_exposure_flows_down_but_child_exposure_not_up_or_sideways(self):
        projection = self.projection()
        parent = [{"concept_id": "bear", "status": "published", "issue_id": "old"}]
        child = [{"concept_id": "bird", "status": "published", "issue_id": "old"}]
        self.assertEqual(_published(parent, aliases=projection), {"bear", "bird", "cat"})
        self.assertEqual(_published(child, aliases=projection), {"bird"})
        self.assertEqual(_published(parent, exclude_issue_id="old", aliases=projection), set())

    def test_child_hold_blocks_parent_but_not_sibling(self):
        hold = [{"issue_id": "queued", "state": "uploaded", "products": [{"concept_id": "bird"}]}]
        self.assertEqual(_held(hold, aliases=self.projection()), {"bear", "bird"})
        self.assertEqual(_held(hold, own_issue_id="queued", aliases=self.projection()), set())

    def test_parent_hold_blocks_both_narrower_children(self):
        hold = [{"issue_id": "queued", "state": "reserved", "products": [{"concept_id": "bear"}]}]
        self.assertEqual(_held(hold, aliases=self.projection()), {"bear", "bird", "cat"})

    def test_partial_history_keeps_only_the_genuinely_unreconciled_hold(self):
        hold = [{"issue_id": "old", "state": "published", "products": [
            {"concept_id": "bear"}, {"concept_id": "other"}]}]
        exposed = [{"issue_id": "old", "concept_id": "bear", "status": "published"}]
        self.assertEqual(_held(hold, aliases=self.projection(), history=exposed), {"other"})

    def test_allocator_never_assigns_parent_and_child_together_but_allows_siblings(self):
        values = self.candidates()
        result = allocate_issues(values, DATES[:1], [], [], identity_state=self.memory.snapshot())
        ids = {p["concept_id"] for p in result["issues"][0]["products"]}
        self.assertFalse("bear" in ids and bool(ids & {"bird", "cat"}))
        children = [row for row in values if row["concept_id"] != "bear"]
        result = allocate_issues(children, DATES[:1], [], [], identity_state=self.memory.snapshot())
        self.assertTrue({"bird", "cat"}.issubset({p["concept_id"] for p in result["issues"][0]["products"]}))

    def test_reservation_rechecks_a_forged_parent_child_selection(self):
        values = self.candidates()
        state = self.memory.snapshot()
        result = allocate_issues(values, DATES[:1], [], [], identity_state=state)
        issue = deepcopy(result["issues"][0])
        issue["products"][:2] = deepcopy(values[:2])
        issue["identity_context_hash"] = selected_alias_binding(state,
            [p["concept_id"] for p in issue["products"]], aliases=self.projection())
        issue["evidence_fingerprint"] = _fingerprint(issue["products"])
        issue["selection_hash"] = selection_fingerprint(issue)
        before = self.memory.snapshot()
        with self.assertRaisesRegex(SafetyError, "ISSUE_DUPLICATE_FUNCTIONAL_IDENTITY"):
            reserve_issue(self.memory, issue, expected_input_hash=issue["input_hash"],
                          expected_snapshot_hash=allocation_snapshot_hash(state))
        self.assertEqual(before, self.memory.snapshot())

