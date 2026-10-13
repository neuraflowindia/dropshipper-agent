"""Real Python owners → real Worker mutation → original stored-byte readback.

Only synthetic captures and an in-memory R2 stand-in are used. No network,
credentials, production migration, automatic task or client release is touched.
"""

import base64
from copy import deepcopy
import json
import subprocess
import unittest

import test_functional_aliases as aliases_fixture
import test_functional_lineage as lineage_fixture
from product_radar.functional_aliases import portable_alias_proof
from product_radar.functional_lineage_portable import portable_lineage_proof, lineage_key
from product_radar.models import SafetyError, canonical, digest
from product_radar.release_index import (apply_reviewed_release_aliases,
    apply_reviewed_release_lineage, _v2_selected_history)
from product_radar.release_pages import ReleasePages
from test_release_index import AFTER, BEFORE, SCHEDULE, entry
from test_release_index_v2 import INDEX, ROOT, MigratedStore


_WORKER = r"""
import fs from 'node:fs';
import {webcrypto,createHash} from 'node:crypto';
if(!globalThis.crypto)globalThis.crypto=webcrypto;
const {handleAdminRequest}=await import(process.argv[1]);
const f=JSON.parse(fs.readFileSync(0,'utf8')),objects=new Map(),calls=[];
for(const [key,value] of Object.entries(f.objects))objects.set(key,{
  bytes:new Uint8Array(Buffer.from(value,'base64')),
  etag:key===f.key?f.etag:'original-'+createHash('sha256').update(Buffer.from(value,'base64')).digest('hex')});
const bucket={
 async get(key){calls.push(['get',key]);const row=objects.get(key);return row&&{
  size:row.bytes.length,etag:row.etag,arrayBuffer:async()=>row.bytes.slice().buffer};},
 async put(key,raw,options={}){calls.push(['put',key]);const old=objects.get(key),c=options.onlyIf;
  if(c instanceof Headers&&c.get('If-None-Match')==='*'&&old||c?.etagMatches!==undefined&&c.etagMatches!==old?.etag)return null;
  const bytes=new Uint8Array(raw).slice(),etag='stored-'+createHash('sha256').update(bytes).digest('hex');
  objects.set(key,{bytes,etag});return {etag};}
};
const env={CLIENT_ID:'default',ACCESS_AUD:'aud',OPERATOR_EMAILS:'["owner@example.com"]',
 ALLOWED_EMAILS:'["client@example.com"]',PUBLICATION_TIME:'09:00',ONGOING_LANGUAGE:'en',REPORTS:bucket};
const request=new Request('https://private.example.com/admin/index',{method:'POST',
 headers:{'X-NeuraFlow-Release-Action':'v1','Content-Type':'application/json'},
 body:JSON.stringify({contract:'release-index-v2',expected_etag:f.expected,mutation:f.mutation})});
const response=await handleAdminRequest(request,env,
 {access:{aud:'aud',getIdentity:async()=>({email:'owner@example.com'})}},
 {clock:()=>new Date(f.now)});
const text=await response.text();
console.log(JSON.stringify({status:response.status,body:JSON.parse(text),calls,
 objects:Object.fromEntries([...objects].map(([key,row])=>[key,Buffer.from(row.bytes).toString('base64')])),
 etag:objects.get(f.key).etag,index:JSON.parse(new TextDecoder().decode(objects.get(f.key).bytes))}));
"""


class ActualWorkerStore(MigratedStore):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.after_mutation = None
        self.worker_calls = []

    def compare_and_swap_index_v2(self, key, expected_etag, mutation):
        self.calls.append(("actual_worker_cas", expected_etag))
        objects = {**self.objects, key: canonical(self.index) + b"\n"}
        payload = {"key": key, "expected": expected_etag, "etag": self.etag,
            "now": self.now, "mutation": mutation,
            "objects": {name: base64.b64encode(raw).decode("ascii") for name, raw in objects.items()}}
        result = subprocess.run(["node", "--input-type=module", "-e", _WORKER,
            (ROOT / "portal/release-admin.mjs").as_uri()], input=canonical(payload),
            capture_output=True, timeout=30)
        if result.returncode:
            raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
        answer = json.loads(result.stdout)
        self.worker_calls = answer["calls"]
        self.objects = {name: base64.b64decode(raw) for name, raw in answer["objects"].items()}
        self.index, self.etag = answer["index"], answer["etag"]
        if answer["status"] != 200:
            raise SafetyError(answer["body"]["error"])
        if self.after_mutation is not None:
            self.after_mutation(self)
            answer["body"].update(index=deepcopy(self.index), etag=self.etag)
        return answer["body"]


def approval(action, proof):
    return {"approved": True, "owner_id": "synthetic-owner", "client_id": "default",
            "action": action, "proof_hash": digest(proof)}


class ReleaseIdentityOwnerJoinTests(unittest.TestCase):
    def alias_case(self):
        fixture = aliases_fixture.FunctionalAliasTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        rid = fixture.accept()["record_id"]
        return fixture, rid

    def lineage_case(self, *, chain=False, siblings=False):
        fixture = lineage_fixture.FunctionalLineageTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        ids = [fixture.accept()["record_id"]]
        if chain: ids.append(fixture.accept("bird", "cat")["record_id"])
        if siblings: ids.append(fixture.accept("bear", "cat")["record_id"])
        return fixture, ids

    def apply_lineage(self, store, fixture, ids, *, expected=None):
        state = fixture.memory.snapshot()
        proof = portable_lineage_proof(state, ids)
        return apply_reviewed_release_lineage(store, state, "default", ids,
            expected_etag=store.etag if expected is None else expected,
            approval=approval("lineage", proof))

    def test_actual_alias_mutation_original_readback_and_stale_replay(self):
        fixture, rid = self.alias_case(); state = fixture.memory.snapshot()
        proof = portable_alias_proof(state, [rid]); store = ActualWorkerStore()
        old = store.etag
        result = apply_reviewed_release_aliases(store, state, "default", [rid],
            expected_etag=old, approval=approval("aliases", proof))
        self.assertTrue(result["mutation_applied"])
        self.assertTrue(result["component_complete"])
        self.assertEqual(result["remaining_record_ids"], [])
        replay = apply_reviewed_release_aliases(store, state, "default", [rid],
            expected_etag=old, approval=approval("aliases", proof))
        self.assertTrue(replay["replay"])
        self.assertFalse(any(op == "put" for op, _ in store.worker_calls))

    def test_actual_chain_mutation_original_readback_and_stale_replay(self):
        fixture, ids = self.lineage_case(chain=True); store = ActualWorkerStore()
        old = store.etag
        result = self.apply_lineage(store, fixture, ids)
        self.assertEqual(result["selected_record_ids_verified"], sorted(ids))
        self.assertEqual(store.index["pages"]["lineage"]["count"], 4)
        replay = self.apply_lineage(store, fixture, ids, expected=old)
        self.assertTrue(replay["replay"])
        self.assertFalse(any(op == "put" for op, _ in store.worker_calls))

    def test_real_parent_exposure_flows_down_but_child_exposure_does_not_flow_up(self):
        fixture, ids = self.lineage_case(siblings=True)
        for exposed, selected, expected in (("bear", "bird", {"bird"}),
                ("bird", "bear", set()), ("bird", "cat", set())):
            with self.subTest(exposed=exposed, selected=selected):
                past = entry("past", 1, SCHEDULE)
                past["concept_ids"][0] = exposed
                store = ActualWorkerStore([past], now=AFTER)
                self.apply_lineage(store, fixture, ids)
                seen, held = _v2_selected_history(store.index, [{"concept_id": selected}],
                    "next", AFTER, store.read_object)
                self.assertEqual(seen, expected)
                self.assertEqual(held, set())

    def test_real_future_holds_overlap_parent_and_child_not_siblings(self):
        fixture, ids = self.lineage_case(siblings=True)
        for held, selected, expected in (("bear", "bird", {"bird"}),
                ("bird", "bear", {"bear"}), ("bird", "cat", set())):
            with self.subTest(held=held, selected=selected):
                future = entry("future", 1, SCHEDULE)
                future["concept_ids"][0] = held
                store = ActualWorkerStore([future], now=BEFORE)
                self.apply_lineage(store, fixture, ids)
                seen, queued = _v2_selected_history(store.index, [{"concept_id": selected}],
                    "next", BEFORE, store.read_object)
                self.assertEqual(seen, set())
                self.assertEqual(queued, expected)

    def test_readback_refuses_missing_actual_original(self):
        fixture, ids = self.lineage_case(); store = ActualWorkerStore()
        def damage(actual):
            actual.objects[f"clients/default/release-index-v2/lineage-records/{ids[0]}.json"] = b"{}"
        store.after_mutation = damage
        with self.assertRaisesRegex(SafetyError, "LINEAGE_READBACK_REQUIRED"):
            self.apply_lineage(store, fixture, ids)

    def test_readback_refuses_paired_duplicate_conflicting_original_refs(self):
        fixture, ids = self.lineage_case(); store = ActualWorkerStore()
        def damage(actual):
            tree = ReleasePages(actual.index, actual.read_object,
                lambda key, raw, **metadata: actual.objects.__setitem__(key, raw))
            row = tree.get("lineage", lineage_key("bear", "bird"))
            row["record_refs"].append({"id": ids[0], "hash": "f" * 64})
            for direction in (0, 1): tree.set("lineage", lineage_key("bear", "bird", direction), row)
            tree.persist(); actual.index = tree.root
        store.after_mutation = damage
        with self.assertRaisesRegex(SafetyError, "LINEAGE_READBACK_REQUIRED"):
            self.apply_lineage(store, fixture, ids)


if __name__ == "__main__":
    unittest.main()
