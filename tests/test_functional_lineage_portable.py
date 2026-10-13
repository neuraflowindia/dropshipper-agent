"""Actual saved narrowing originals and offline Python/Worker agreement."""

from copy import deepcopy
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import test_functional_lineage as lineage_fixture
from test_release_pages import Store
from product_radar.frozen_records import freeze_original_json, read_original_json
from product_radar.functional_lineage_portable import (lineage_key, lineage_neighbors,
    lineage_record_capsule, lineage_type_context, portable_lineage_proof, validate_lineage_proof,
    validate_lineage_record_capsule)
from product_radar.models import SafetyError, canonical, digest
from product_radar.release_pages import KINDS, ReleasePages, empty_release_root, validate_release_root
from product_radar.storage import atomic_json


NOW = "2026-10-09T10:00:00+00:00"
AT = lineage_fixture.AT


class PortableLineageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = lineage_fixture.FunctionalLineageTests()
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.accepted = self.fixture.accept()
        self.memory = self.fixture.memory

    def proof(self, ids=None):
        return portable_lineage_proof(self.memory.snapshot(), ids or [self.accepted["record_id"]])

    def test_real_originals_round_trip_without_mutation_or_invented_association_table(self):
        state = self.memory.snapshot(); before = canonical(state)
        proof = self.proof(); checked = validate_lineage_proof(proof, as_of=NOW)
        self.assertEqual(set(proof), {"contract", "records", "definitions", "observations", "descriptions"})
        self.assertEqual(set(checked["records"]), {self.accepted["record_id"]})
        self.assertEqual(checked["proof_hash"], digest(proof))
        for name, table in (("records", "source_checks"), ("definitions", "source_checks"),
                            ("observations", "observations"), ("descriptions", "descriptions")):
            for rid, wrapper in proof[name].items():
                self.assertEqual(read_original_json(wrapper), state[table][rid])
                self.assertEqual(wrapper["sha256"], digest(state[table][rid]))
        self.assertEqual(canonical(self.memory.snapshot()), before)

    def test_historical_capsule_stays_byte_exact_after_later_meaning_heading_or_association_change(self):
        original = self.proof()
        self.fixture.fixture.accept_definition("bird", dict(self.fixture.base, mechanism="Later accepted mechanism"),
            reviewed_at="2026-10-06T12:00:00+00:00")
        aid = self.fixture.fixture.fixture.children["bird"]
        before = self.memory.snapshot()["associations"][aid]
        self.memory.put("associations", aid, dict(before, reason="Later legitimate annotation"), previous=before)
        self.assertEqual(self.proof(), original)
        self.assertEqual(validate_lineage_proof(original, as_of=NOW)["proof_hash"], digest(original))

    def test_multiple_selected_edges_share_exact_originals_and_only_their_required_chains(self):
        other = self.fixture.accept("bird", "cat")
        ids = [self.accepted["record_id"], other["record_id"]]
        proof = self.proof(ids)
        self.assertEqual(set(validate_lineage_proof(proof, as_of=NOW)["records"]), set(ids))
        self.assertEqual(proof, self.proof(list(reversed(ids))))
        state = self.memory.snapshot()
        self.assertEqual(set(proof["observations"]), {
            self.fixture.fixture.fixture.observations[cid] for cid in ("bear", "bird", "cat")})
        bound = state["source_checks"][self.accepted["record_id"]]["input"]["definition_input"]["definition_bindings"]["bird"]["definition_id"]
        self.assertGreater(read_original_json(proof["definitions"][bound])["revision"], 1)

    def test_fixed_per_edge_capsules_have_only_their_actual_original_subset(self):
        other = self.fixture.accept("bird", "cat")
        proof = self.proof([self.accepted["record_id"], other["record_id"]])
        for rid in proof["records"]:
            capsule = lineage_record_capsule(proof, rid, as_of=NOW)
            self.assertEqual(capsule, lineage_record_capsule(self.proof([rid]), rid, as_of=NOW))
            checked = validate_lineage_record_capsule(capsule, as_of=NOW,
                expected_record_id=rid, expected_hash=proof["records"][rid]["sha256"])
            self.assertEqual(set(checked["records"]), {rid})
            with self.assertRaises(SafetyError):
                validate_lineage_record_capsule(capsule, as_of=NOW, expected_record_id="wrong")
            with self.assertRaises(SafetyError):
                validate_lineage_record_capsule(capsule, as_of=NOW, expected_hash="0" * 64)

    def test_explicit_selection_is_bounded_and_never_silently_truncated(self):
        rid = self.accepted["record_id"]
        for ids in ([], [rid, rid], ["missing"], [rid] * 21, "not-a-list"):
            with self.subTest(ids=ids):
                with self.assertRaises(SafetyError): portable_lineage_proof(self.memory.snapshot(), ids)

    def test_twenty_one_real_edges_use_explicit_twenty_plus_one_batches(self):
        ids = [self.accepted["record_id"]]
        prepared = self.fixture.prepared()
        for n in range(20):
            result = lineage_fixture.accept_functional_lineage(self.memory, prepared,
                reviewer="synthetic_operator", reason="Actual reviewed boundary " + str(n),
                distinctions=["A narrower shelf compatibility boundary."])
            ids.append(result["record_id"])
        self.assertEqual(len(set(ids)), 21)
        with self.assertRaisesRegex(SafetyError, "SELECTION_INVALID"): self.proof(ids)
        self.assertEqual(set(self.proof(ids[:20])["records"]), set(ids[:20]))
        self.assertEqual(set(self.proof(ids[20:])["records"]), set(ids[20:]))

    def test_producer_checks_exact_size_incrementally_without_writing_or_reading_all_closure(self):
        before = canonical(self.memory.snapshot()); original = self.proof()
        module = "product_radar.functional_lineage_portable."
        with patch(module + "MAX_PROOF_BYTES", len(canonical(original))):
            self.assertEqual(self.proof(), original)
        with patch(module + "MAX_PROOF_BYTES", len(canonical(original)) - 1):
            with self.assertRaisesRegex(SafetyError, "BATCH_REQUIRED"): self.proof()
        with patch(module + "MAX_PROOF_BYTES", 64), patch(module + "freeze_original_json", wraps=freeze_original_json) as freeze:
            with self.assertRaisesRegex(SafetyError, "BATCH_REQUIRED"): self.proof()
            self.assertEqual(freeze.call_count, 1)
        self.assertEqual(canonical(self.memory.snapshot()), before)

    def test_changed_frozen_original_without_matching_hash_is_refused(self):
        for table in ("records", "definitions", "observations", "descriptions"):
            proof = self.proof(); rid = next(iter(proof[table]))
            proof[table][rid]["canonical_json"] += " "
            with self.subTest(table=table):
                with self.assertRaisesRegex(SafetyError, "FROZEN_ORIGINAL"):
                    validate_lineage_proof(proof, as_of=NOW)

    def test_changed_grounding_even_with_rehashed_wrapper_is_refused(self):
        proof = self.proof(); oid = next(iter(proof["observations"]))
        value = read_original_json(proof["observations"][oid])
        value["observed_at"] = NOW
        proof["observations"][oid] = freeze_original_json(value)
        with self.assertRaisesRegex(SafetyError, "GROUNDING_CHANGED"):
            validate_lineage_proof(proof, as_of=NOW)

    def test_missing_description_and_invented_child_proof_are_not_accepted(self):
        proof = self.proof(); proof["descriptions"].pop(next(iter(proof["descriptions"])))
        with self.assertRaisesRegex(SafetyError, "GROUNDING_CHANGED"):
            validate_lineage_proof(proof, as_of=NOW)
        proof = self.proof(); sid = next(iter(proof["descriptions"]))
        value = read_original_json(proof["descriptions"][sid]); value["result"]["products"] = []
        proof["descriptions"][sid] = freeze_original_json(value)
        with self.assertRaisesRegex(SafetyError, "GROUNDING_CHANGED"):
            validate_lineage_proof(proof, as_of=NOW)

    def test_missing_predecessor_cannot_be_replaced_by_only_latest_definition(self):
        proof = self.proof()
        first = next(did for did, w in proof["definitions"].items()
                     if read_original_json(w)["concept_id"] == "bird" and read_original_json(w)["revision"] == 1)
        proof["definitions"].pop(first)
        with self.assertRaisesRegex(SafetyError, "REVISION_CONFLICT"):
            validate_lineage_proof(proof, as_of=NOW)

    def test_extra_originals_and_later_definition_heads_cannot_extend_fixed_capsule(self):
        for table in ("observations", "descriptions"):
            proof = self.proof(); proof[table]["unrelated"] = next(iter(proof[table].values()))
            with self.subTest(table=table):
                with self.assertRaisesRegex(SafetyError, "UNUSED_ORIGINAL"):
                    validate_lineage_proof(proof, as_of=NOW)
        self.fixture.fixture.accept_definition("bird", dict(self.fixture.base, mechanism="A revised mechanism"),
            reviewed_at="2026-10-06T12:00:00+00:00")
        proof = self.proof(); state = self.memory.snapshot()
        for did, row in state["source_checks"].items():
            if row.get("kind") == "functional-definition-v1" and row["concept_id"] == "bird":
                proof["definitions"][did] = freeze_original_json(row)
        with self.assertRaisesRegex(SafetyError, "UNUSED_ORIGINAL"):
            validate_lineage_proof(proof, as_of=NOW)

    def test_future_review_or_definition_does_not_become_past_proof(self):
        with self.assertRaisesRegex(SafetyError, "FUTURE"):
            validate_lineage_proof(self.proof(), as_of="2026-10-05T11:00:00+00:00")
        for at in (None, AT.replace("+00:00", "+05:30"), "not-a-date"):
            with self.subTest(at=at):
                with self.assertRaises(SafetyError): validate_lineage_proof(self.proof(), as_of=at)

    def test_oversized_real_original_requires_an_explicit_smaller_batch(self):
        proof = self.proof(); oid = next(iter(proof["observations"]))
        value = read_original_json(proof["observations"][oid]); value["unused_text"] = "x" * (480 * 1024)
        proof["observations"][oid] = freeze_original_json(value)
        with self.assertRaisesRegex(SafetyError, "BATCH_REQUIRED"):
            validate_lineage_proof(proof, as_of=NOW)

    def test_actual_worker_reads_python_originals_with_identical_proof_hash(self):
        proof = self.proof()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "proof.json"; atomic_json(path, proof)
            module = (Path(__file__).resolve().parents[1] / "portal/lineage-proof.mjs").as_uri()
            script = """import fs from 'node:fs';import{webcrypto}from'node:crypto';
if(!globalThis.crypto)globalThis.crypto=webcrypto;
const m=await import(process.argv[1]),p=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const checked=await m.validateLineageProof(p,{asOf:process.argv[3]});
if(checked.proof_hash!==process.argv[4])throw Error('Python/Worker proof hash differs');
"""
            result = subprocess.run(["node", "--input-type=module", "-e", script, module, str(path), NOW, digest(proof)],
                                    capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode())


class DirectionalReleaseReaderTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.tree = ReleasePages(empty_release_root("default", NOW), self.store.read, self.store.put)

    def edge(self, parent, child, *, rid=None, hash=None):
        value = {"parent_id": parent, "child_id": child,
                 "record_refs": [{"id": rid or "edge_" + parent + "_" + child, "hash": hash or digest([parent, child])}]}
        for direction in (0, 1): self.tree.set("lineage", lineage_key(parent, child, direction), value)
        return value

    def test_absent_lineage_reads_are_empty_and_do_not_expand_legacy_root(self):
        original = deepcopy(self.tree.root)
        self.assertEqual(self.tree.get("lineage", lineage_key("parent", "child")), None)
        self.assertEqual(self.tree.scan("lineage")["rows"], [])
        self.tree.set("lineage", lineage_key("parent", "child"), None)
        self.assertEqual(self.tree.root, original)
        self.assertEqual(set(self.tree.root["pages"]), set(KINDS))
        self.assertEqual(lineage_type_context(self.tree, "child"), {"canonical_id": "child", "member_ids": ["child"],
            "ancestor_ids": [], "descendant_ids": [], "lineage_refs": []})

    def test_only_explicit_mutation_adds_ninth_kind_and_preserves_old_root(self):
        original = deepcopy(self.tree.root); self.edge("parent", "child"); self.tree.persist()
        self.assertEqual(set(validate_release_root(self.tree.root)["pages"]), set(KINDS) | {"lineage"})
        self.assertEqual(ReleasePages(original, self.store.read).scan("lineage")["rows"], [])
        for kind in KINDS: self.assertEqual(original["pages"][kind], self.tree.root["pages"][kind])

    def test_child_history_and_parent_holds_direction_do_not_union_siblings(self):
        self.edge("parent", "child"); self.edge("parent", "sibling"); self.edge("child", "grandchild")
        child = lineage_type_context(self.tree, "child")
        self.assertEqual(child["member_ids"], ["child"])
        self.assertEqual(child["ancestor_ids"], ["parent"])
        self.assertEqual(child["descendant_ids"], ["grandchild"])
        self.assertNotIn("sibling", child["ancestor_ids"] + child["descendant_ids"])
        self.assertEqual(lineage_type_context(self.tree, "parent")["descendant_ids"], ["child", "grandchild", "sibling"])

    def test_real_alias_group_expands_reachable_ancestors_without_equating_siblings(self):
        self.edge("parent", "child"); self.edge("parent", "sibling")
        for cid in ("parent", "equivalent"):
            self.tree.set("aliases", digest(cid), {"member_id": cid, "canonical_id": "equivalent",
                "member_ids": ["equivalent", "parent"], "ancestor_ids": [], "record_refs": []})
        child = lineage_type_context(self.tree, "child")
        self.assertEqual(child["ancestor_ids"], ["equivalent", "parent"])
        self.assertEqual(child["member_ids"], ["child"])

    def test_malformed_alias_order_canonical_ancestry_or_refs_cannot_change_history_lookup(self):
        self.edge("parent", "child")
        valid = {"member_id": "parent", "canonical_id": "equivalent", "member_ids": ["equivalent", "parent"],
                 "ancestor_ids": [], "record_refs": [{"id": "original", "hash": digest("original")}]}
        for changes in ({"member_ids": ["parent", "equivalent"]}, {"canonical_id": "parent"},
                        {"ancestor_ids": ["invented"]}, {"record_refs": valid["record_refs"] * 2},
                        {"record_refs": [{"id": "original", "hash": digest("original"), "extra": True}]}):
            self.tree.set("aliases", digest("parent"), dict(valid, **changes))
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(SafetyError, "ALIAS_PROJECTION_INVALID"):
                    lineage_type_context(self.tree, "child")

    def test_missing_or_different_reciprocal_alias_leaf_is_not_a_real_group(self):
        self.edge("parent", "child")
        valid = {"member_id": "parent", "canonical_id": "equivalent", "member_ids": ["equivalent", "parent"],
                 "ancestor_ids": [], "record_refs": []}
        self.tree.set("aliases", digest("parent"), valid)
        with self.assertRaisesRegex(SafetyError, "ALIAS_PROJECTION_INVALID"):
            lineage_type_context(self.tree, "child")
        self.tree.set("aliases", digest("equivalent"), dict(valid, member_id="equivalent", record_refs=[
            {"id": "different", "hash": digest("different")}]))
        with self.assertRaisesRegex(SafetyError, "ALIAS_PROJECTION_INVALID"):
            lineage_type_context(self.tree, "child")

    def test_missing_or_changed_mirror_is_not_silent_absence(self):
        row = self.edge("parent", "child")
        self.tree.set("lineage", lineage_key("parent", "child", 1), None)
        with self.assertRaisesRegex(SafetyError, "MIRROR_INVALID"): lineage_neighbors(self.tree, "parent", 0)
        self.tree.set("lineage", lineage_key("parent", "child", 1), dict(row, record_refs=[]))
        with self.assertRaisesRegex(SafetyError, "MIRROR_INVALID"): lineage_neighbors(self.tree, "parent", 0)

    def test_reachable_cycles_and_alias_collapsed_edges_are_refused(self):
        self.edge("parent", "child"); self.edge("child", "parent")
        with self.assertRaisesRegex(SafetyError, "CYCLE"): lineage_type_context(self.tree, "parent")
        self.tree.set("lineage", lineage_key("child", "parent", 0), None)
        self.tree.set("lineage", lineage_key("child", "parent", 1), None)
        overrides = {cid: {"member_id": cid, "canonical_id": "child", "member_ids": ["child", "parent"],
            "record_refs": [], "ancestor_ids": []} for cid in ("child", "parent")}
        with self.assertRaisesRegex(SafetyError, "CYCLE"):
            lineage_type_context(self.tree, "parent", projection_overrides=overrides)

    def test_conflicting_record_hash_or_duplicate_projection_refs_are_refused(self):
        self.edge("parent", "child", rid="same_original")
        self.edge("child", "grandchild", rid="same_original", hash="0" * 64)
        with self.assertRaisesRegex(SafetyError, "RECORD_CHANGED"): lineage_type_context(self.tree, "child")
        row = self.edge("another", "last"); row["record_refs"] *= 2
        for direction in (0, 1): self.tree.set("lineage", lineage_key("another", "last", direction), row)
        with self.assertRaisesRegex(SafetyError, "PROJECTION_INVALID"): lineage_neighbors(self.tree, "another", 0)

    def test_bounded_adjacency_scan_over_multiple_pages_and_worker_context_match(self):
        # Offline bulk history fixture, not 115 updates in one hosted request.
        rows = []
        for n in range(115):
            parent, child = "parent", "child_" + str(n)
            value = {"parent_id": parent, "child_id": child, "record_refs": [
                {"id": "edge_" + child, "hash": digest([parent, child])}]}
            for direction in (0, 1): rows.append({"key": lineage_key(parent, child, direction), "value": value})
        self.tree.root["pages"]["lineage"] = self.tree.build("lineage", sorted(rows, key=lambda r: r["key"]))
        self.tree.persist()
        expected = lineage_type_context(self.tree, "parent")
        self.assertEqual(len(expected["descendant_ids"]), 115)
        self.assertEqual(len(self.tree.root["pages"]["lineage"]["sha256"]), 64)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.json"
            atomic_json(path, {"root": self.tree.root, "objects": {k: v.decode() for k, v in self.store.objects.items()}})
            module = (Path(__file__).resolve().parents[1] / "portal/release-history.mjs").as_uri()
            script = """import fs from'node:fs';import{webcrypto}from'node:crypto';if(!globalThis.crypto)globalThis.crypto=webcrypto;
const h=await import(process.argv[1]),p=await import(new URL('./release-pages.mjs',process.argv[1]));
const f=JSON.parse(fs.readFileSync(process.argv[2],'utf8')),b={get:async k=>({size:new TextEncoder().encode(f.objects[k]).length,arrayBuffer:async()=>new TextEncoder().encode(f.objects[k]).buffer})};
console.log(JSON.stringify(await h.lineageTypeContext(new p.ReleaseTree(b,f.root),'parent')));
"""
            result = subprocess.run(["node", "--input-type=module", "-e", script, module, str(path)], capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            from product_radar.models import decode_json
            self.assertEqual(decode_json(result.stdout), expected)


if __name__ == "__main__": unittest.main()
