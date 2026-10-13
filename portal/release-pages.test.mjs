import test from 'node:test';
import assert from 'node:assert/strict';
import { ReleaseTree, emptyReleaseRoot, releaseDigest, releaseBytes, releaseHash, releaseSequenceKey,
  validateReleaseRoot, releaseCursor, readReleaseCursor, parseRelease } from './release-pages.mjs';

const CLIENT = 'fixture-client', AT = '2026-10-09T10:00:00.000Z';
function bucket() {
  const objects = new Map(), calls = [];
  return { objects, calls, async get(key) { calls.push(['get', key]); const bytes = objects.get(key); return bytes && { size: bytes.length, arrayBuffer: async () => bytes.slice().buffer }; },
    async put(key, bytes, options) { calls.push(['put', key]); if (objects.has(key) && options?.onlyIf?.get?.('If-None-Match') === '*') return null;
      objects.set(key, bytes.slice()); return { etag: 'fixture-etag' }; } };
}

test('logical ID keys match Python canonical digest, not raw text hashes', async () => {
  // Independently generated with product_radar.models.digest, including Unicode.
  for (const [value, expected] of [
    ['sink-caddy', '4d27221d2ad49b0387b2d95f712b8e4593f6ea25b818559d714e602c44bb29f7'],
    ['नमस्ते', 'edf96c10c150674cdf9e71d5d0d3dbbf7f22857c8cf7bd51a00554cdfc351b9a'],
    ['type-🙂', 'ad4557ec3528c3ba2a2d45d350e7987fadbd5e80b3f4b0123717f8dac7594707'],
  ]) {
    assert.equal(await releaseDigest(value), expected);
  }
});

test('20,000 lifetime types have a small root and bounded single-type lookup', async () => {
  const store = bucket(), root = emptyReleaseRoot(CLIENT, AT), builder = new ReleaseTree(store, root, { writeLimit: 5000 });
  const entries = await Promise.all(Array.from({ length: 20000 }, async (_, i) => ({ key: await releaseDigest('type-' + i), value: { concept_id: 'type-' + i, first_exposed_at: AT } })));
  entries.sort((a, b) => a.key.localeCompare(b.key)); root.pages.types = await builder.build('types', entries); await builder.persist();
  validateReleaseRoot(root, CLIENT, AT);
  assert.ok(releaseBytes(root).length < 2000); assert.equal(root.pages.types.count, 20000);
  const reader = new ReleaseTree(store, root, { readLimit: 8 });
  assert.equal((await reader.get('types', await releaseDigest('type-19876'))).concept_id, 'type-19876');
  assert.ok(reader.reads <= 5);
  for (const bytes of store.objects.values()) assert.ok(bytes.length <= 256 * 1024);
});

test('2,000 archive entries paginate completely without scanning all pages per request', async () => {
  const store = bucket(), root = emptyReleaseRoot(CLIENT, AT), builder = new ReleaseTree(store, root, { writeLimit: 1000 });
  const entries = Array.from({ length: 2000 }, (_, i) => ({ key: releaseSequenceKey(i + 1), value: { issue_id: 'issue-' + i, sequence: i + 1 } }));
  root.pages.archive = await builder.build('archive', entries);
  const revisions = await Promise.all(entries.map(async row => ({ key: await releaseDigest(row.value.issue_id) + releaseSequenceKey(1), value: { issue_id: row.value.issue_id, revision: 1, sequence: row.value.sequence } })));
  revisions.sort((a, b) => a.key.localeCompare(b.key)); root.pages.revisions = await builder.build('revisions', revisions);
  root.next_sequence = 2001; await builder.persist();
  validateReleaseRoot(root, CLIENT, AT);
  const found = [], reads = []; let after = null;
  do { const reader = new ReleaseTree(store, root, { readLimit: 30 }), result = await reader.scan('archive', { after, reverse: true, limit: 20 });
    found.push(...result.rows.map(row => row.value.sequence)); reads.push(reader.reads); after = result.more ? result.last_key : null;
  } while (after);
  assert.deepEqual(found, Array.from({ length: 2000 }, (_, i) => 2000 - i)); assert.ok(Math.max(...reads) <= 23);
});

test('copy-on-write changes only the relevant path and preserves old immutable pages', async () => {
  const store = bucket(), root = emptyReleaseRoot(CLIENT, AT), builder = new ReleaseTree(store, root);
  const rows = await Promise.all(Array.from({ length: 100 }, async (_, i) => ({ key: await releaseDigest('issue-' + i), value: { issue_id: 'issue-' + i, state: 'active' } })));
  rows.sort((a, b) => a.key.localeCompare(b.key)); root.pages.issues = await builder.build('issues', rows); await builder.persist();
  const old = structuredClone(root), original = new Map([...store.objects].map(([k, b]) => [k, b.slice()]));
  const next = structuredClone(root); next.generation++; next.previous_root_hash = await releaseDigest(root);
  const writer = new ReleaseTree(store, next); await writer.set('issues', await releaseDigest('issue-9'), { issue_id: 'issue-9', state: 'withdrawn' }); await writer.persist();
  assert.ok(writer.pending.size <= 3);
  assert.equal((await new ReleaseTree(store, old).get('issues', await releaseDigest('issue-9'))).state, 'active');
  assert.equal((await new ReleaseTree(store, next).get('issues', await releaseDigest('issue-9'))).state, 'withdrawn');
  for (const [key, bytes] of original) assert.deepEqual(store.objects.get(key), bytes);
});

test('same-root cursor is navigation only and stale/different-client cursors refuse', async () => {
  const root = emptyReleaseRoot(CLIENT, AT), token = await releaseCursor(root, 'archive', releaseSequenceKey(10), { reverse: true, prefix: '' });
  assert.equal((await readReleaseCursor(token, root, 'archive')).last_key, releaseSequenceKey(10));
  const next = { ...root, generation: 2 };
  await assert.rejects(readReleaseCursor(token, next, 'archive'), /CURSOR_STALE/);
  await assert.rejects(readReleaseCursor(token, { ...root, client_id: 'other' }, 'archive'), /CURSOR_STALE/);
  await assert.rejects(readReleaseCursor(token, root, 'issues'), /CURSOR_STALE/);
});

test('unrelated missing page does not prevent a proved independent lookup', async () => {
  const store = bucket(), root = emptyReleaseRoot(CLIENT, AT), builder = new ReleaseTree(store, root);
  const entries = Array.from({ length: 60 }, (_, i) => ({ key: (i < 30 ? 'a' : 'b') + i.toString(16).padStart(63, '0'), value: { concept_id: 'type-' + i } }));
  root.pages.types = await builder.build('types', entries); await builder.persist();
  const top = parseRelease(store.objects.get(root.pages.types.key)); store.objects.delete(top.children.b.key);
  assert.equal((await new ReleaseTree(store, root).get('types', entries[0].key)).concept_id, 'type-0');
  await assert.rejects(new ReleaseTree(store, root).get('types', entries.at(-1).key), /OBJECT_UNAVAILABLE/);
});

test('forged binding, counts, hash and duplicate JSON cannot become a trusted page', async () => {
  const store = bucket(), root = emptyReleaseRoot(CLIENT, AT), builder = new ReleaseTree(store, root);
  const key = await releaseDigest('type'); await builder.set('types', key, { concept_id: 'type' }); await builder.persist();
  const ref = root.pages.types, original = store.objects.get(ref.key);
  store.objects.set(ref.key, releaseBytes({ ...parseRelease(original), client_id: 'other-client' }));
  await assert.rejects(new ReleaseTree(store, root).get('types', key), /HASH_INVALID/);
  const page = { ...parseRelease(original), client_id: 'other-client' }, bytes = releaseBytes(page), sha256 = await releaseHash(bytes);
  const other = { ...ref, sha256, size: bytes.length, key: ref.key.replace(ref.sha256, sha256) }; store.objects.set(other.key, bytes);
  await assert.rejects(new ReleaseTree(store, { ...root, pages: { ...root.pages, types: other } }).get('types', key), /BINDING_INVALID/);
  assert.throws(() => parseRelease(new TextEncoder().encode('{"a":1,"a":2}')), /DUPLICATE/);
});

test('read and write limits are enforced before unbounded work', async () => {
  const store = bucket(), root = emptyReleaseRoot(CLIENT, AT), builder = new ReleaseTree(store, root);
  await builder.set('types', await releaseDigest('type'), { concept_id: 'type' }); await builder.persist();
  await assert.rejects(new ReleaseTree(store, root, { readLimit: 0 }).get('types', await releaseDigest('type')), /READ_BUDGET/);
  await assert.rejects(new ReleaseTree(store, root, { writeLimit: 0 }).set('types', await releaseDigest('new'), { concept_id: 'new' }), /WRITE_BUDGET/);
});
