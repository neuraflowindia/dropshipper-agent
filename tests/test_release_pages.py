"""Bounded actual immutable objects and Python/Worker byte agreement."""

from copy import deepcopy
from pathlib import Path
import subprocess
import tempfile
import unittest

from product_radar.models import SafetyError, canonical, digest
from product_radar.release_pages import (ReleasePages, empty_release_root, read_release_cursor,
    release_bytes, release_cursor, sequence_key, validate_release_root)
from product_radar.storage import atomic_json

NOW = "2026-10-09T10:00:00+00:00"


class Store:
    def __init__(self): self.objects = {}; self.reads = 0
    def read(self, key): self.reads += 1; return self.objects[key]
    def put(self, key, raw, **kwargs):
        if key in self.objects and self.objects[key] != raw: raise SafetyError("IMMUTABLE_CONFLICT")
        self.objects[key] = raw


class ReleasePageTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.tree = ReleasePages(empty_release_root("default", NOW), self.store.read, self.store.put)

    def seed(self, count=2000, kind="archive"):
        # This explicit offline bulk fixture stages objects over the whole
        # test, not one hosted request. Online readers retain their bounds.
        self.tree.write_limit = 10000
        entries = [{"key": sequence_key(n) if kind == "archive" else digest("type-" + str(n)),
                    "value": {"issue_id": "issue-" + str(n), "sequence": n, "label": "सीधा उपयोग"}}
                   for n in range(1, count + 1)]
        entries.sort(key=lambda r: r["key"])
        self.tree.root["pages"][kind] = self.tree.build(kind, entries)
        if kind == "archive":
            self.tree.root["next_sequence"] = count + 1
            revisions = sorted([{"key": digest("issue-" + str(n)) + sequence_key(1),
                "value": {"issue_id": "issue-" + str(n), "revision": 1, "sequence": n}}
                for n in range(1, count + 1)], key=lambda row: row["key"])
            self.tree.root["pages"]["revisions"] = self.tree.build("revisions", revisions)
        self.tree.persist()
        return entries

    def test_two_thousand_archive_rows_read_in_bounded_pages_and_reverse_order(self):
        expected = self.seed()
        tree = ReleasePages(self.tree.root, self.store.read)
        first = tree.scan("archive", limit=20, reverse=True)
        self.assertEqual([r["value"]["sequence"] for r in first["rows"]], list(range(2000, 1980, -1)))
        self.assertTrue(first["more"])
        self.assertLessEqual(tree.reads, 23)
        token = release_cursor(tree.root, "archive", first["last_key"], reverse=True)
        cursor = read_release_cursor(token, tree.root, "archive")
        second = tree.scan("archive", after=cursor["last_key"], reverse=cursor["reverse"], limit=20)
        self.assertEqual(second["rows"], list(reversed(expected[-40:-20])))

    def test_twenty_thousand_types_keep_small_root_and_small_single_id_lookup(self):
        self.seed(20000, "types")
        self.assertLess(len(release_bytes(self.tree.root)), 2048)
        tree = ReleasePages(self.tree.root, self.store.read)
        self.assertEqual(tree.get("types", digest("type-19999"))["sequence"], 19999)
        self.assertLessEqual(tree.reads, 5)
        self.assertTrue(all(len(raw) <= 256 * 1024 for raw in self.store.objects.values()))

    def test_incremental_changes_keep_unrelated_objects_and_old_root_readable(self):
        self.seed(150)
        original = deepcopy(self.tree.root); original_objects = dict(self.store.objects)
        later = ReleasePages(original, self.store.read, self.store.put)
        later.root["generation"] += 1; later.root["previous_root_hash"] = digest(original)
        later.set("archive", sequence_key(151), {"issue_id": "new", "sequence": 151})
        later.set("revisions", digest("new") + sequence_key(1), {"issue_id": "new", "revision": 1, "sequence": 151})
        later.root["next_sequence"] = 152
        later.persist()
        self.assertEqual(ReleasePages(original, self.store.read).get("archive", sequence_key(151)), None)
        self.assertEqual(later.get("archive", sequence_key(151))["issue_id"], "new")
        self.assertTrue(all(self.store.objects[k] == v for k, v in original_objects.items()))
        self.assertLess(len(later.pending), 20)

    def test_corrupt_or_missing_original_page_is_not_an_empty_history(self):
        self.seed(2)
        ref = self.tree.root["pages"]["archive"]
        self.store.objects[ref["key"]] = b"{}\n"
        with self.assertRaisesRegex(SafetyError, "PAGE_HASH_INVALID"):
            ReleasePages(self.tree.root, self.store.read).get("archive", sequence_key(1))
        del self.store.objects[ref["key"]]
        with self.assertRaises(KeyError): ReleasePages(self.tree.root, self.store.read).get("archive", sequence_key(1))

    def test_stale_foreign_client_and_tampered_cursor_cannot_navigate_old_root(self):
        self.seed(2); token = release_cursor(self.tree.root, "archive", sequence_key(1))
        later = deepcopy(self.tree.root); later["generation"] += 1
        with self.assertRaisesRegex(SafetyError, "CURSOR_STALE"): read_release_cursor(token, later, "archive")
        other = deepcopy(self.tree.root); other["client_id"] = "other"
        with self.assertRaisesRegex(SafetyError, "PAGE_REFERENCE_INVALID"): validate_release_root(other)
        with self.assertRaisesRegex(SafetyError, "CURSOR_STALE"): read_release_cursor(token, self.tree.root, "types")

    def test_explicit_read_and_write_limits_do_not_spin_or_silently_drop_pages(self):
        self.seed(100)
        with self.assertRaisesRegex(SafetyError, "READ_BUDGET"):
            ReleasePages(self.tree.root, self.store.read, read_limit=1).get("archive", sequence_key(99))
        with self.assertRaisesRegex(SafetyError, "WRITE_BUDGET"):
            ReleasePages(self.tree.root, self.store.read, write_limit=1).build("archive",
                [{"key": sequence_key(n), "value": {"n": n}} for n in range(1, 101)])

    def test_json_types_and_duplicate_keys_are_not_coerced(self):
        for value in (1.0, float("nan"), 2**53, "\ud800"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(SafetyError): release_bytes(value)
        root = deepcopy(self.tree.root); root["schema_version"] = True
        with self.assertRaises(SafetyError): validate_release_root(root)

    def test_python_objects_and_cursor_are_read_by_actual_worker_module(self):
        self.seed(101)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "fixture.json"
            atomic_json(path, {"root": self.tree.root, "objects": {k: v.decode() for k, v in self.store.objects.items()},
                "cursor": release_cursor(self.tree.root, "archive", sequence_key(90), reverse=True)})
            module = (Path(__file__).resolve().parents[1] / "portal/release-pages.mjs").as_uri()
            script = """import fs from 'node:fs';
import { webcrypto } from 'node:crypto'; if(!globalThis.crypto)globalThis.crypto = webcrypto;
const m = await import(process.argv[1]); const f=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const b={get:async k=>({size:new TextEncoder().encode(f.objects[k]).length,arrayBuffer:async()=>new TextEncoder().encode(f.objects[k]).buffer})};
const tree=new m.ReleaseTree(b,f.root); const row=await tree.get('archive',m.releaseSequenceKey(100));
if(row.sequence!==100)throw new Error('wrong original row');
const cursor=await m.readReleaseCursor(f.cursor,f.root,'archive');
const page=await tree.scan('archive',{after:cursor.last_key,reverse:true,limit:20});
if(page.rows[0].value.sequence!==89||page.rows.length!==20)throw new Error('wrong continuation');
if(await m.releaseDigest(f.root)!==process.argv[3])throw new Error('canonical hash mismatch');
"""
            result = subprocess.run(["node", "--input-type=module", "-e", script, module, str(path), digest(self.tree.root)],
                                    capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))


if __name__ == "__main__": unittest.main()
