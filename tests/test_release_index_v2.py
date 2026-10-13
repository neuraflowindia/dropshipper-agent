"""Actual Worker migration bytes and synthetic private storage only.

These tests do not open a connection, publish a client report or change a task.
The migration planner is the real JavaScript module; the injected Python store
uses the existing v1 transition only to simulate an authenticated server write.
"""

import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from product_radar.models import SafetyError, canonical, digest
from product_radar.portal_storage import DraftAutomationStorage, PortalResponse, PortalStorage
from product_radar.release_index import (activate_delivery_package, apply_release_mutation,
    plan_activation, project_issue_ordinal, publication_receipt, release_receipt,
    validate_release_index, withdraw_release)
from product_radar.release_pages import (ReleasePages, read_release_cursor, release_cursor,
    sequence_key)
from product_radar.storage import load_json
from test_release_index import (AFTER, BEFORE, SCHEDULE, approved, entry, prepared, revised)

ROOT = Path(__file__).resolve().parents[1]
INDEX = "clients/default/release-index.json"
TOKEN = "manufacturedheader.manufacturedpayload.manufacturedsignature"
ORIGIN = "https://reports.example.com"
_MIGRATIONS = {}


def migrate(index, *, as_of=BEFORE, facts=None):
    """Read genuine planReleaseMigration pages, not assumed page structures."""
    payload = {"index": index, "at": as_of, "facts": facts or {}}
    cache_key = digest(payload)
    if cache_key not in _MIGRATIONS:
        script = """import fs from 'node:fs';
import {webcrypto} from 'node:crypto'; if (!globalThis.crypto) globalThis.crypto=webcrypto;
const m=await import(process.argv[1]), f=JSON.parse(fs.readFileSync(0,'utf8'));
const p=await m.planReleaseMigration(f.index,{client:'default',sourceEtag:'legacy-etag',asOf:f.at,factsByRevision:f.facts});
console.log(JSON.stringify({index:p.root,objects:Object.fromEntries(p.objects.map(o=>[o.key,Buffer.from(o.bytes).toString('base64')]))}));
"""
        result = subprocess.run(["node", "--input-type=module", "-e", script,
            (ROOT / "portal/release-admin.mjs").as_uri()], input=canonical(payload),
            capture_output=True, timeout=30)
        if result.returncode:
            raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
        parsed = json.loads(result.stdout)
        _MIGRATIONS[cache_key] = (parsed["index"], {
            key: base64.b64decode(value) for key, value in parsed["objects"].items()})
    return deepcopy(_MIGRATIONS[cache_key])


def facts_for(entries):
    return {row["issue_id"] + ":" + str(row["revision"]): {
        "issue_id": row["issue_id"], "revision": row["revision"],
        "selection_hash": row["selection_hash"],
        "products": [{"concept_id": cid, "status": "New"} for cid in row["concept_ids"]]}
        for row in entries}


class MigratedStore:
    """Fake server observations with actual migration-generated page bytes."""

    def __init__(self, entries=(), *, now=BEFORE, facts=None):
        self.legacy = {"schema_version": 1, "client_id": "default", "issues": deepcopy(list(entries))}
        self.facts = deepcopy(facts if facts is not None else facts_for(entries))
        self.index, self.objects = migrate(self.legacy, as_of=now, facts=self.facts)
        self.now, self.etag, self.version = now, "v2-etag-1", 1
        self.calls, self.fail_after_commit = [], False

    def read_index(self, key):
        self.calls.append(("read_index", key))
        return {"index": deepcopy(self.index), "etag": self.etag, "verified": True,
            "authority": "release_index", "server_observed_at": self.now}

    def read_object(self, key):
        self.calls.append(("read_object", key))
        return self.objects[key]

    def put_immutable(self, key, raw, **kwargs):
        self.calls.append(("put", key))
        if key in self.objects and self.objects[key] != raw:
            raise SafetyError("FIXTURE_IMMUTABLE_CONFLICT")
        self.objects[key] = raw

    def compare_and_swap_index_v2(self, key, expected_etag, mutation):
        self.calls.append(("cas_v2", expected_etag))
        if expected_etag != self.etag:
            return None
        self.legacy = apply_release_mutation(self.legacy, mutation, server_at=self.now)
        if mutation["kind"] == "activate":
            manifest = mutation["manifest"]
            path = f"clients/default/issues/{manifest['issue_id']}/r{manifest['revision']}/final_report_data.json"
            self.facts[manifest["issue_id"] + ":" + str(manifest["revision"])] = json.loads(self.objects[path])
        old = self.index
        self.index, pages = migrate(self.legacy, as_of=self.now, facts=self.facts)
        self.index.update(generation=old["generation"] + 1, previous_root_hash=digest(old))
        self.objects.update(pages)
        self.version += 1
        self.etag = "v2-etag-" + str(self.version)
        if self.fail_after_commit:
            self.fail_after_commit = False
            raise SafetyError("FIXTURE_LOST_SERVER_RESPONSE")
        return {**self.read_index(key), "mutation_applied": True}


class HistoryTransport:
    """Owner response payloads are verified again against immutable pages."""

    def __init__(self, store, *, alter=None, ignore_cursor=False):
        self.store, self.alter, self.calls = store, alter, []
        self.ignore_cursor = ignore_cursor

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        path = urlsplit(url)
        if path.path == "/admin/index-page":
            values = parse_qs(path.query)
            kind = values["kind"][0]
            tree = ReleasePages(self.store.index, self.store.read_object)
            result = {**self.store.read_index(INDEX), "kind": kind}
            if "id" in values:
                item_id = values["id"][0]
                key = digest(item_id)
                if kind == "revisions": key += sequence_key(int(values["revision"][0]))
                value = tree.get(kind, key)
                if kind == "revisions" and value:
                    value = tree.get("archive", sequence_key(value["sequence"]))
                result["value"] = value
            else:
                cursor = values.get("cursor", [None])[0]
                parsed = read_release_cursor(cursor, tree.root, kind) if cursor and not self.ignore_cursor else None
                options = {"after": parsed["last_key"], "reverse": parsed["reverse"], "prefix": parsed["prefix"]} if parsed else {"reverse": kind in ("archive", "timeline")}
                page = tree.scan(kind, limit=20, **options)
                result.update(rows=page["rows"], next_cursor=release_cursor(tree.root, kind,
                    page["last_key"], reverse=options.get("reverse", False), prefix=options.get("prefix", "")) if page["more"] else None)
            if self.alter: self.alter(result)
            raw = canonical(result)
        elif path.path.startswith("/admin/release-pages/"):
            key = "clients/default/release-index-v2/pages/" + path.path[len("/admin/release-pages/"):]
            raw = self.store.read_object(key)
        elif path.path == "/admin/index":
            raw = canonical(self.store.read_index(INDEX))
        else:
            raise AssertionError("Unexpected route: " + path.path)
        return PortalResponse(200, {"content-type": "application/json", "content-length": str(len(raw)),
            "x-content-sha256": hashlib.sha256(raw).hexdigest()}, raw)


class ReleaseIndexV2Tests(unittest.TestCase):
    def test_actual_migration_pages_match_python_current_root_and_receipt(self):
        row = entry("already-published", 1, SCHEDULE)
        row["facts_hash"] = digest("exact delivered facts")
        store = MigratedStore([row], now=AFTER)
        validate_release_index(store.index, "default", server_at=AFTER)
        receipt = release_receipt(store.read_index(INDEX), row["issue_id"], 1, read_object=store.read_object)
        self.assertTrue(receipt["published"])
        self.assertEqual(receipt["facts_hash"], row["facts_hash"])
        self.assertEqual(receipt["package_hash"], row["package_hash"])
        self.assertEqual(store.index["settled_publication_count"], 1)

    def test_older_revision_receipt_survives_a_newer_future_revision(self):
        old = entry("correction", 1, SCHEDULE)
        old.update(remote_state="withdrawn", server_deactivated_at=AFTER)
        new = entry("correction", 1, "2026-10-15T02:30:00+00:00", revision=2)
        new["server_activated_at"] = AFTER
        store = MigratedStore([old, new], now=AFTER)
        first = release_receipt(store.read_index(INDEX), "correction", 1, read_object=store.read_object)
        second = release_receipt(store.read_index(INDEX), "correction", 2, read_object=store.read_object)
        self.assertTrue(first["published"])
        self.assertEqual(first["remote_state"], "withdrawn")
        self.assertFalse(second["published"])
        self.assertEqual(first["publication_ordinal"], second["publication_ordinal"])
        self.assertIsNone(publication_receipt(store.read_index(INDEX), "correction", 2, read_object=store.read_object))

    def test_queue_ordinal_counts_elapsed_new_issues_not_revisions(self):
        first = entry("first", 1, SCHEDULE)
        first.update(remote_state="withdrawn", server_deactivated_at=AFTER)
        corrected = entry("first", 1, "2026-10-15T02:30:00+00:00", revision=2)
        corrected["server_activated_at"] = AFTER
        next_row = entry("second", 2, "2026-10-17T02:30:00+00:00")
        store = MigratedStore([first, corrected, next_row], now=AFTER)
        self.assertEqual(project_issue_ordinal(store.index, "first", corrected["scheduled_at"], server_at=AFTER, read_object=store.read_object), 1)
        self.assertEqual(project_issue_ordinal(store.index, "third", "2026-10-20T02:30:00+00:00", server_at=AFTER, read_object=store.read_object), 3)

    def test_unsettled_elapsed_queue_is_counted_before_next_release(self):
        store = MigratedStore([entry("first", 1, SCHEDULE)])
        store.now = AFTER
        self.assertEqual(store.index["settled_publication_count"], 0)
        self.assertEqual(project_issue_ordinal(store.index, "next", "2026-10-15T02:30:00+00:00", server_at=AFTER, read_object=store.read_object), 2)

    def test_actual_package_activation_and_stale_exact_replay_do_not_reupload(self):
        with tempfile.TemporaryDirectory() as root:
            issue, manifest = prepared(root)
            store = MigratedStore()
            original_etag = store.etag
            result = activate_delivery_package(store, issue, manifest, root=root, approval=approved(manifest),
                scheduled_at=SCHEDULE, expected_etag=original_etag, allow_synthetic=True)
            self.assertFalse(result["published"])
            self.assertEqual(store.index["next_sequence"], 2)
            store.calls.clear()
            replay = activate_delivery_package(store, issue, manifest, root=root, approval=approved(manifest),
                scheduled_at=SCHEDULE, expected_etag=original_etag, allow_synthetic=True)
            self.assertTrue(replay["replay"])
            self.assertFalse(any(call[0] in ("put", "cas_v2") for call in store.calls))

    def test_future_own_issue_correction_does_not_treat_unexposed_type_as_invalid_history(self):
        with tempfile.TemporaryDirectory() as root:
            issue, manifest = prepared(root)
            store = MigratedStore()
            activate_delivery_package(store, issue, manifest, root=root, approval=approved(manifest),
                scheduled_at=SCHEDULE, expected_etag=store.etag, allow_synthetic=True)
            changed, replacement = revised(root, issue, manifest)
            result = activate_delivery_package(store, changed, replacement, root=root, approval=approved(replacement),
                scheduled_at=SCHEDULE, expected_etag=store.etag, allow_synthetic=True,
                expected_previous={"revision": 1, "package_hash": manifest["package_hash"]})
            self.assertEqual(result["receipt"]["revision"], 2)
            self.assertFalse(result["published"])
            old = release_receipt(store.read_index(INDEX), issue["issue_id"], 1, read_object=store.read_object)
            self.assertFalse(old["published"])

    def test_stale_etag_for_an_unseen_issue_is_a_conflict_without_upload_or_missing_revision_error(self):
        with tempfile.TemporaryDirectory() as root:
            issue, manifest = prepared(root)
            store = MigratedStore()
            with self.assertRaisesRegex(SafetyError, "^RELEASE_INDEX_CONFLICT$"):
                activate_delivery_package(store, issue, manifest, root=root, approval=approved(manifest),
                    scheduled_at=SCHEDULE, expected_etag="a stale root", allow_synthetic=True)
            self.assertFalse(any(call[0] in ("put", "cas_v2") for call in store.calls))

    def test_lost_withdrawal_response_recovers_same_original_with_old_etag(self):
        row = entry("already-published", 1, SCHEDULE)
        store = MigratedStore([row], now=AFTER)
        original_etag = store.etag
        approval = {"approved": True, "owner_id": "owner-one", "client_id": "default",
            "issue_id": row["issue_id"], "revision": 1, "package_hash": row["package_hash"], "action": "withdraw"}
        store.fail_after_commit = True
        with self.assertRaisesRegex(SafetyError, "FIXTURE_LOST_SERVER_RESPONSE"):
            withdraw_release(store, "default", row["issue_id"], 1, row["package_hash"], approval=approval, expected_etag=original_etag)
        store.calls.clear()
        recovered = withdraw_release(store, "default", row["issue_id"], 1, row["package_hash"], approval=approval, expected_etag=original_etag)
        self.assertEqual(recovered["receipt"]["remote_state"], "withdrawn")
        self.assertTrue(recovered["published"])
        self.assertFalse(any(call[0] == "cas_v2" for call in store.calls))

    def test_owner_history_page_and_revision_lookup_rederive_actual_saved_pages(self):
        rows = [entry("published-" + str(i), i, SCHEDULE) for i in range(1, 26)]
        store = MigratedStore(rows, now=AFTER)
        transport = HistoryTransport(store)
        owner = PortalStorage(ORIGIN, "default", TOKEN, transport=transport)
        first = owner.read_index_page("archive")
        self.assertEqual(len(first["rows"]), 20)
        second = owner.read_index_page("archive", cursor=first["next_cursor"])
        self.assertEqual(len(second["rows"]), 5)
        self.assertFalse(set(row["key"] for row in first["rows"]) & set(row["key"] for row in second["rows"]))
        result = owner.read_index_page("revisions", item_id="published-1", revision=1)
        self.assertEqual(result["value"]["issue_id"], "published-1")
        self.assertIsNone(owner.read_index_page("revisions", item_id="published-1", revision=2)["value"])

    def test_supplied_history_payload_or_orphan_page_is_not_authority(self):
        store = MigratedStore([entry("published", 1, SCHEDULE)], now=AFTER)
        for alter in (lambda value: value["rows"][0]["value"].update(title="forged title"),
                      lambda value: value["rows"].append({"key": sequence_key(99), "value": {"issue_id": "orphan"}})):
            owner = PortalStorage(ORIGIN, "default", TOKEN, transport=HistoryTransport(store, alter=alter))
            with self.assertRaisesRegex(SafetyError, "PORTAL_HISTORY_RESPONSE_INVALID"):
                owner.read_index_page("archive")

    def test_changed_root_makes_prior_history_cursor_stale(self):
        store = MigratedStore([entry("published-" + str(i), i, SCHEDULE) for i in range(1, 26)], now=AFTER)
        owner = PortalStorage(ORIGIN, "default", TOKEN, transport=HistoryTransport(store))
        token = owner.read_index_page("archive")["next_cursor"]
        store.index["generation"] += 1
        # An invalid 200 reply cannot make an old cursor current again. A real
        # server may instead return its named 409 before this local check.
        owner = PortalStorage(ORIGIN, "default", TOKEN, transport=HistoryTransport(store, ignore_cursor=True))
        with self.assertRaisesRegex(SafetyError, "RELEASE_CURSOR_STALE"):
            owner.read_index_page("archive", cursor=token)

    def test_draft_service_cannot_fetch_owner_pages_or_use_v2_mutation(self):
        store = MigratedStore()
        transport = HistoryTransport(store)
        draft = DraftAutomationStorage(ORIGIN, "default", "a" * 32 + ".access", "b" * 64, transport=transport)
        for call in (lambda: draft.read_index_page("archive"),
                     lambda: draft.compare_and_swap_index_v2(INDEX, store.etag, {"kind": "reconcile", "client_id": "default"}),
                     lambda: draft.read_object("clients/default/release-index-v2/pages/archive/" + "a" * 64 + ".json")):
            with self.assertRaisesRegex(SafetyError, "PORTAL_DRAFT_ONLY_ROUTE_REQUIRED"):
                call()
        self.assertEqual(transport.calls, [])

    def test_owner_v2_cas_preserves_explicit_contract_and_actual_server_continuation(self):
        store = MigratedStore()
        result = {**store.read_index(INDEX), "mutation_applied": False,
            "settlement": {"settled": 20, "remaining": True}, "next_action": "reconcile"}
        class Transport:
            def __init__(self): self.calls = []
            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                raw = canonical(result)
                return PortalResponse(200, {"content-type": "application/json", "content-length": str(len(raw))}, raw)
        transport = Transport()
        owner = PortalStorage(ORIGIN, "default", TOKEN, transport=transport)
        mutation = {"kind": "reconcile", "client_id": "default"}
        observed = owner.compare_and_swap_index_v2(INDEX, store.etag, mutation)
        self.assertEqual(observed, result)
        method, url, actual = transport.calls[0]
        self.assertEqual((method, url), ("POST", ORIGIN + "/admin/index"))
        self.assertEqual(json.loads(actual["body"]), {"contract": "release-index-v2",
            "expected_etag": store.etag, "mutation": mutation})
        self.assertEqual(len(transport.calls), 1)

    def test_owner_page_transport_checks_original_page_hash_not_just_supplied_header(self):
        store = MigratedStore([entry("published", 1, SCHEDULE)], now=AFTER)
        page = store.index["pages"]["archive"]
        # The synthetic server claims this changed body using its genuine new
        # body hash. It still cannot match the original page's hash/name.
        store.objects[page["key"]] = b"{}\n"
        owner = PortalStorage(ORIGIN, "default", TOKEN, transport=HistoryTransport(store))
        with self.assertRaisesRegex(SafetyError, "PORTAL_OBJECT_VERIFICATION_FAILED"):
            owner.read_object(page["key"])


class ReviewedAliasAcceptanceTests(unittest.TestCase):
    """Actual local reviewed edges; never real remote mutations or publication."""

    def alias_fixture(self):
        from test_functional_aliases import FunctionalAliasTests
        fixture = FunctionalAliasTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_alias_success_requires_actual_alias_projection_not_only_success_flag(self):
        from product_radar.functional_aliases import portable_alias_proof
        from product_radar.release_index import apply_reviewed_release_aliases
        fixture = self.alias_fixture()
        accepted = fixture.accept()
        state = fixture.memory.snapshot()
        proof = portable_alias_proof(state, [accepted["record_id"]])
        store = MigratedStore()
        # A broken authenticated adapter claims success but has not changed
        # any root, page or immutable alias original. Read-back stays genuine.
        store.compare_and_swap_index_v2 = lambda key, etag, mutation: {
            **store.read_index(key), "mutation_applied": True}
        approval = {"approved": True, "owner_id": "synthetic_owner", "client_id": "default",
            "action": "aliases", "proof_hash": digest(proof)}
        with self.assertRaises(SafetyError):
            apply_reviewed_release_aliases(store, state, "default", [accepted["record_id"]],
                expected_etag=store.etag, approval=approval)

    def test_real_legacy_identity_ancestry_does_not_block_new_portable_alias(self):
        from test_identity_corrections import IdentityCorrectionTests, NOW
        from product_radar.identity_corrections import apply_unallocated_identity_merge
        from product_radar.functional_aliases import (accept_functional_alias,
            alias_projection, portable_alias_proof, prepare_alias_input)
        from product_radar.functional_types import accept_functional_definition, effective_functional_definition
        fixture = IdentityCorrectionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        apply_unallocated_identity_merge(fixture.memory, fixture.make_request(("bear", "cat")))
        definition = {"report_title": "Decorative bookends", "primary_use": "Hold books upright",
            "mechanism": "Fixed side supports", "use_context": ["Book shelf"],
            "compatibility_constraints": [], "included_distinctions": ["Decorative themes"],
            "excluded_distinctions": ["Powered mechanism"]}
        for cid in ("bear", "bird"):
            accept_functional_definition(fixture.memory, cid, definition,
                basis_refs=[fixture.observations[cid]], reviewer="synthetic_operator", reviewed_at=NOW,
                expected_meaning_hash=effective_functional_definition(fixture.memory.snapshot(), cid)["meaning_hash"])
        prepared = prepare_alias_input(fixture.memory.snapshot(), ["bear", "bird"], reviewed_at=NOW)
        accepted = accept_functional_alias(fixture.memory, prepared, method="exact_meaning",
            reviewer="exact_saved_meanings", reason="The actual narrow definitions match.")
        state = fixture.memory.snapshot()
        self.assertEqual(alias_projection(state)["bird"]["member_ids"], ["bear", "bird", "cat"])
        proof = portable_alias_proof(state, [accepted["record_id"]])
        self.assertTrue(proof["records"])

    def test_twenty_one_accepted_edges_use_explicit_twenty_plus_one_batches(self):
        from product_radar.functional_aliases import accept_functional_alias, portable_alias_proof
        fixture = self.alias_fixture()
        prepared = fixture.prepared()
        record_ids = []
        for index in range(21):
            result = accept_functional_alias(fixture.memory, prepared, method="exact_meaning",
                reviewer="synthetic_operator", reason="Actual saved equivalence review " + str(index))
            record_ids.append(result["record_id"])
        state = fixture.memory.snapshot()
        self.assertEqual(len(set(record_ids)), 21)
        first = portable_alias_proof(state, record_ids[:20])
        second = portable_alias_proof(state, record_ids[20:])
        self.assertEqual(set(first["records"]), set(record_ids[:20]))
        self.assertEqual(set(second["records"]), {record_ids[20]})
        for rid in (record_ids[0], record_ids[-1]):
            self.assertEqual(set(portable_alias_proof(state, [rid])["records"]), {rid})


if __name__ == "__main__":
    unittest.main()
