import test from 'node:test';
import assert from 'node:assert/strict';
import { ReleaseTree, releaseDigest, releaseSequenceKey, validateReleaseRoot } from './release-pages.mjs';
import { buildReleaseHistory, selectedTypeHistory, settleReleaseQueue, visibleReleasePage, archiveEntry, issueProjection } from './release-history.mjs';
import { validateReleaseIndex } from './release-admin.mjs';
const CLIENT = 'history-client', NOW = '2026-10-09T10:00:00.000Z', FUTURE = '2026-10-10T03:30:00.000Z', LATER = '2026-10-11T10:00:00.000Z';
function store() { const objects = new Map(); return { objects, async get(key) { const bytes = objects.get(key); return bytes && { size: bytes.length, arrayBuffer: async () => bytes.slice().buffer }; },
  async put(key, value) { if (!objects.has(key)) objects.set(key, value.slice()); return { etag: 'fixture' }; } }; }
function entry(id = 'one', { ordinal = 1, at = '2026-10-08T03:30:00.000Z', activated = '2026-10-07T10:00:00.000Z', revision = 1, withdrawn = null, concepts = null } = {}) {
  const languages = ordinal <= 2 ? ['en', 'hi-Latn'] : ['en'];
  return { issue_id: id, revision, publication_ordinal: ordinal, remote_state: withdrawn ? 'withdrawn' : 'active', approved: true, complete: true,
    package_hash: 'a'.repeat(64), selection_hash: 'b'.repeat(64), facts_hash: 'c'.repeat(64), title: 'Manufactured history fixture',
    scheduled_at: at, server_activated_at: activated, ...(withdrawn ? { server_deactivated_at: withdrawn } : {}),
    product_count: 10, new_count: 10, concept_ids: concepts ?? Array.from({ length: 10 }, (_, i) => id + '-type-' + i), languages,
    files: ['shortlist.xlsx', ...languages.map(l => `report-${l}.pdf`)].map(name => ({ name, key: `clients/${CLIENT}/issues/${id}/r${revision}/${name}`,
      sha256: 'd'.repeat(64), size: 10, media_type: name.endsWith('.pdf') ? 'application/pdf' : 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' })) };
}
function facts(row) { return { issue_id: row.issue_id, revision: row.revision, selection_hash: row.selection_hash,
  products: row.concept_ids.map(concept_id => ({ concept_id, status: 'New' })) }; }
const validateEntry = (e, at) => validateReleaseIndex({ schema_version: 1, client_id: CLIENT, issues: [e] }, CLIENT, at);
async function fixture(entries, asOf = NOW) { const bucket = store(), factsByRevision = Object.fromEntries(entries.map(row => [row.issue_id + ':' + row.revision, facts(row)]));
  const planned = await buildReleaseHistory(entries, { client: CLIENT, asOf, factsByRevision }); planned.tree.bucket = bucket; await planned.tree.persist();
  return { ...planned, bucket, reader: () => new ReleaseTree(bucket, structuredClone(planned.root), { readLimit: 400, writeLimit: 400 }) }; }

test('permanent exposure survives withdrawal; a withdrawn pre-release hold does not create exposure', async () => {
  const old = entry('old', { withdrawn: '2026-10-09T08:00:00.000Z' }), never = entry('never', { at: FUTURE, withdrawn: '2026-10-09T08:00:00.000Z' });
  const f = await fixture([old, never]); validateReleaseRoot(f.root, CLIENT, NOW);
  assert.equal(f.root.settled_publication_count, 1);
  const history = await selectedTypeHistory(f.reader(), [old.concept_ids[0], never.concept_ids[0]], { issueId: 'next', at: NOW, validateEntry });
  assert.deepEqual([...history.published], [old.concept_ids[0]]); assert.equal(history.queued.size, 0);
});

test('a corrected original issue keeps its New labels without resetting exposure for other issues', async () => {
  const row = entry('original'), f = await fixture([row]);
  assert.equal((await selectedTypeHistory(f.reader(), [row.concept_ids[0]], { issueId: 'original', at: NOW, validateEntry })).published.size, 0);
  assert.equal((await selectedTypeHistory(f.reader(), [row.concept_ids[0]], { issueId: 'other', at: NOW, validateEntry })).published.size, 1);
});

test('future queue is an exact hold now and genuine exposure after time passes, without laptop execution', async () => {
  const row = entry('future', { at: FUTURE }), f = await fixture([row]);
  const before = await selectedTypeHistory(f.reader(), [row.concept_ids[0]], { issueId: 'next', at: NOW, validateEntry });
  assert.equal(before.queued.size, 1); assert.equal(before.published.size, 0);
  const after = await selectedTypeHistory(f.reader(), [row.concept_ids[0]], { issueId: 'next', at: LATER, validateEntry });
  assert.equal(after.published.size, 1); assert.equal(after.queued.size, 0);
  const tree = f.reader(), settled = await settleReleaseQueue(tree, { at: LATER, validateEntry });
  assert.equal(settled.settled, 1); assert.equal(tree.root.settled_publication_count, 1); assert.equal(tree.root.pages.holds, null);
  tree.root.updated_at = LATER; await tree.persist(); validateReleaseRoot(tree.root, CLIENT, LATER);
  assert.equal((await issueProjection(tree, 'future', validateEntry, LATER)).row.publication_ordinal, 1);
  assert.equal((await settleReleaseQueue(tree, { at: LATER, validateEntry })).settled, 0);
});

test('queue settlement is bounded and explicitly reports remaining work', async () => {
  const entries = Array.from({ length: 21 }, (_, i) => entry('future-' + i, { ordinal: i + 1, at: FUTURE })), f = await fixture(entries), tree = f.reader();
  const first = await settleReleaseQueue(tree, { at: LATER, validateEntry, limit: 20 });
  assert.equal(first.settled, 20); assert.equal(first.remaining, true); assert.ok(first.next_cursor);
  const second = await settleReleaseQueue(tree, { at: LATER, validateEntry, limit: 20 });
  assert.equal(second.settled, 1); assert.equal(second.remaining, false); assert.equal(tree.root.settled_publication_count, 21);
});

test('an old issue correction does not replace a newer scheduled issue as latest', async () => {
  const a = entry('old', { withdrawn: '2026-10-09T08:00:00.000Z' }), b = entry('new', { ordinal: 2, at: '2026-10-09T03:30:00.000Z' }), correction = entry('old', { revision: 2, activated: '2026-10-09T08:00:00.000Z' });
  const f = await fixture([a, b, correction]), rows = await visibleReleasePage(f.reader(), { at: NOW, validateEntry });
  assert.deepEqual(rows.entries.map(row => [row.issue_id, row.revision]), [['new', 1], ['old', 2]]);
});

test('future facts are required to derive exact New/Updated holds, never guessed from concept IDs', async () => {
  const future = entry('future', { at: FUTURE });
  await assert.rejects(buildReleaseHistory([future], { client: CLIENT, asOf: NOW }), /HISTORY_FACTS_REQUIRED/);
  const actual = facts(future); actual.products[0].status = 'Updated';
  const plan = await buildReleaseHistory([future], { client: CLIENT, asOf: NOW, factsByRevision: { 'future:1': actual } });
  const bucket = store(); plan.tree.bucket = bucket; await plan.tree.persist();
  const held = await plan.tree.scan('holds', { prefix: await releaseDigest(future.concept_ids[0]), limit: 20 }); assert.equal(held.rows[0].value.status, 'Updated');
});

test('bad current pointer refuses; old immutable active pages alone cannot bypass withdrawal authority', async () => {
  const row = entry(), f = await fixture([row]), tree = f.reader();
  const issue = await tree.get('issues', await releaseDigest(row.issue_id)); issue.active_sequence = 999;
  await tree.set('issues', await releaseDigest(row.issue_id), issue);
  await assert.rejects(issueProjection(tree, row.issue_id, validateEntry, NOW), /POINTER_INVALID/);
  assert.equal((await archiveEntry(f.reader(), 1, validateEntry, NOW)).remote_state, 'active');
});
