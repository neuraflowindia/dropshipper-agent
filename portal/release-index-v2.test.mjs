// Synthetic saved packets/in-memory private R2 only. No hosted or model calls.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { canonical, handleAdminRequest, validateReleaseIndex } from './release-admin.mjs';
import { handleRequest } from './worker.mjs';
import { handleAdminStatus } from './admin-status.mjs';
import { buildReleaseHistory,lineageKey } from './release-history.mjs';
import { releaseBytes, releaseDigest, parseRelease,ReleaseTree } from './release-pages.mjs';
import {actualAliasFixtures} from './alias-fixtures.test-helper.mjs';
import {actualLineageFixtures} from './lineage-fixtures.test-helper.mjs';
const CLIENT = 'v2-client', ROOT = `clients/${CLIENT}/release-index.json`, NOW = '2026-10-09T10:00:00.000Z', AFTER = '2026-10-10T04:00:00.000Z';
const SCHEDULE = '2026-10-10T03:30:00+00:00', enc = new TextEncoder(), sha = bytes => createHash('sha256').update(bytes).digest('hex');
const bytes = value => enc.encode(canonical(value) + '\n');
const ctx = (email = 'owner@example.com') => ({ access: { aud: 'aud', getIdentity: async () => ({ email }) } });
const clock = at => ({ clock: () => new Date(at) });
function bucket() { const objects = new Map(), calls = []; let n = 0; return { objects, calls, failStage: false, conflictRoot: false,
  async get(key) { calls.push(['get', key]); const row = objects.get(key); return row && { size: row.bytes.length, etag: row.etag, arrayBuffer: async () => row.bytes.slice().buffer }; },
  async put(key, raw, options = {}) { calls.push(['put', key]); if (this.failStage && key.includes('/migrations/')) throw new Error('fixture interruption');
    if (this.conflictRoot && key === ROOT) return null;
    const old = objects.get(key), condition = options.onlyIf;
    if (condition instanceof Headers && condition.get('If-None-Match') === '*' && old || condition?.etagMatches !== undefined && condition.etagMatches !== old?.etag) return null;
    const etag = 'etag-' + ++n; objects.set(key, { bytes: new Uint8Array(raw).slice(), etag });
    if (this.loseRootReply && key === ROOT) { this.loseRootReply = false; return null; } return { etag }; } }; }
function env() { return { CLIENT_ID: CLIENT, ACCESS_AUD: 'aud', OPERATOR_EMAILS: '["owner@example.com"]', ALLOWED_EMAILS: '["client@example.com"]',
  PUBLICATION_TIME: '09:00', ONGOING_LANGUAGE: 'en', REPORTS: bucket() }; }
function req(path = '/admin/index', input = null) { return new Request('https://private.example.com' + path, { method: input ? 'POST' : 'GET',
  headers: { 'X-NeuraFlow-Release-Action': 'v1', ...(input ? { 'Content-Type': 'application/json' } : {}) }, ...(input ? { body: bytes(input) } : {}) }); }
async function post(e, mutation, expected = e.REPORTS.objects.get(ROOT)?.etag ?? null, at = NOW) {
  return handleAdminRequest(req('/admin/index', { contract: 'release-index-v2', expected_etag: expected, mutation }), e, ctx(), clock(at)); }
async function migrate(e, finish = true) { return post(e, { kind: 'migrate', client_id: CLIENT, finish }); }
function packet(id = 'one', ordinal = 1, date = '2026-10-10', concepts = null) {
  const languages = ordinal <= 2 ? ['en', 'hi-Latn'] : ['en'], path = `clients/${CLIENT}/issues/${id}/r1/`;
  const products = Array.from({ length: 10 }, (_, i) => ({ concept_id: concepts?.[i] ?? `${id}-type-${i}`, rank: i + 1, title: 'Manufactured fixture',
    category: ['Home and Decor', 'Baby Products', 'Home and Kitchen', 'Travel and Outdoor'][i % 4], status: 'New',
    kpi_states: ['Meets', 'Meets', 'Not assessed', ...Array(5).fill('Meets'), ...Array(5).fill('Unclear')], kpi_matches: 7 }));
  const facts = { schema_version: 1, evidence_type: 'captured', issue_id: id, revision: 1, release_date: date, selection_hash: 'b'.repeat(64), products }, rawFacts = bytes(facts);
  const objects = new Map([[path + 'final_report_data.json', rawFacts]]), artifacts = { facts: { path: '/fixture/final_report_data.json', sha256: sha(rawFacts), size: rawFacts.length } };
  const files = ['shortlist.xlsx', ...languages.map(l => `report-${l}.pdf`)].map(name => { const raw = enc.encode(name.endsWith('.pdf') ? '%PDF-1.7\nfixture' : 'PK fixture'); objects.set(path + name, raw);
    const artifact = { path: '/fixture/' + name, sha256: sha(raw), size: raw.length }; artifacts[name === 'shortlist.xlsx' ? 'workbook' : 'pdf-' + name.slice(7, -4)] = artifact;
    return { name, key: path + name, sha256: artifact.sha256, size: artifact.size, media_type: name.endsWith('.pdf') ? 'application/pdf' : 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }; });
  const manifest = { schema_version: 1, client_id: CLIENT, issue_id: id, revision: 1, release_date: date, evidence_type: 'captured', selection_hash: facts.selection_hash,
    facts_hash: sha(enc.encode(canonical(facts))), complete: true, approved: false, release_ready: false, publication_ordinal: ordinal,
    concept_ids: products.map(p => p.concept_id), counts: { products: 10, new: 10, updates: 0 }, product_count: 10, new_count: 10, languages, files, artifacts };
  manifest.package_hash = sha(enc.encode(canonical(manifest))); objects.set(path + 'delivery-manifest.json', bytes(manifest));
  return { facts, manifest, objects };
}
function activation(p, schedule = SCHEDULE) { const m = p.manifest; return { kind: 'activate', client_id: CLIENT, manifest: m, scheduled_at: schedule, expected_previous: null,
  selected_language: m.publication_ordinal > 2 ? 'en' : null, allow_synthetic: false,
  approval: { approved: true, owner_id: 'owner', client_id: CLIENT, issue_id: m.issue_id, revision: m.revision, package_hash: m.package_hash,
    selection_hash: m.selection_hash, publication_ordinal: m.publication_ordinal, scheduled_at: schedule } }; }
async function stage(e, p) { for (const [key, raw] of p.objects) await e.REPORTS.put(key, raw); }
async function readRoot(e) { return parseRelease(e.REPORTS.objects.get(ROOT).bytes); }
async function client(e, path, at = AFTER) { return handleRequest(new Request('https://private.example.com' + path), e, ctx('client@example.com'), Date.parse(at)); }
async function aliases(proof,dependent_withdrawals) { return {kind:'aliases',client_id:CLIENT,proof,
  approval:{approved:true,owner_id:'owner',client_id:CLIENT,action:'aliases',proof_hash:await releaseDigest(proof)},
  ...(dependent_withdrawals===undefined?{}:{dependent_withdrawals})}; }
async function lineage(proof,dependent_withdrawals){const result=await aliases(proof,dependent_withdrawals);result.kind='lineage';result.approval.action='lineage';return result;}
const withdrawal = p => ({kind:'withdraw',client_id:CLIENT,issue_id:p.manifest.issue_id,revision:p.manifest.revision,package_hash:p.manifest.package_hash,
  approval:{approved:true,owner_id:'owner',client_id:CLIENT,issue_id:p.manifest.issue_id,revision:p.manifest.revision,package_hash:p.manifest.package_hash,action:'withdraw'}});

test('genuine narrowing originals activate in the ninth kind only and exact replay performs no writes',async()=>{
  const e=env();await migrate(e);const old=await readRoot(e),etag=e.REPORTS.objects.get(ROOT).etag;assert.equal(Object.keys(old.pages).length,8);
  const proof=actualLineageFixtures().chain,mutation=await lineage(proof),wrong=await post(e,{...mutation,approval:{...mutation.approval,proof_hash:'f'.repeat(64)}});assert.equal(wrong.status,403);
  const applied=await post(e,mutation);assert.equal(applied.status,200,await applied.clone().text());assert.equal((await applied.json()).index.pages.lineage.count,4);
  const listed=await handleAdminRequest(req('/admin/index-page?kind=lineage&endpoint=bear&direction=0'),e,ctx(),clock(NOW));assert.equal(listed.status,200);const rows=await listed.json();assert.equal(rows.rows.length,1);assert.equal(rows.rows[0].value.child_id,'bird');
  const original=[...e.REPORTS.objects.keys()].find(key=>key.includes('/lineage-records/')),read=await handleAdminRequest(req('/admin/lineage-records/'+original.split('/').at(-1)),e,ctx(),clock(NOW));assert.equal(read.status,200);assert.deepEqual(new Uint8Array(await read.arrayBuffer()),e.REPORTS.objects.get(original).bytes);
  e.REPORTS.calls.length=0;const replay=await post(e,mutation,etag);assert.equal(replay.status,200,await replay.clone().text());assert.equal((await replay.json()).replay,true);assert.ok(!e.REPORTS.calls.some(([op])=>op==='put'));
  e.REPORTS.objects.get(original).bytes=enc.encode('{}');const damaged=await post(e,mutation,etag);assert.equal(damaged.status,409);assert.match(await damaged.text(),/LINEAGE_ORIGINAL_CHANGED/);
});

test('narrowing cannot silently join parent and descendant queues; exact companion withdrawal permits it',async()=>{
  const e=env();await migrate(e);const a=packet('parent',1,'2026-10-10',['bear',...Array.from({length:9},(_,i)=>'parent-peer-'+i)]),b=packet('child',2,'2026-10-13',['bird',...Array.from({length:9},(_,i)=>'child-peer-'+i)]);
  for(const p of [a,b])await stage(e,p);assert.equal((await post(e,activation(a))).status,200);assert.equal((await post(e,activation(b,'2026-10-13T03:30:00+00:00'))).status,200);
  const proof=actualLineageFixtures().one,raw=e.REPORTS.objects.get(ROOT).bytes.slice(),blocked=await post(e,await lineage(proof));assert.equal(blocked.status,409);const error=await blocked.json();assert.equal(error.error,'ADMIN_LINEAGE_QUEUE_OWNER_ACTION_REQUIRED');assert.deepEqual(error.affected_issue_ids,['child','parent']);assert.deepEqual(e.REPORTS.objects.get(ROOT).bytes,raw);
  const applied=await post(e,await lineage(proof,[withdrawal(b)]));assert.equal(applied.status,200,await applied.clone().text());
  const c=packet('child-later',2,'2026-10-13',b.manifest.concept_ids);await stage(e,c);const refused=await post(e,activation(c,'2026-10-13T03:30:00+00:00'));assert.equal(refused.status,409);assert.match(await refused.text(),/FACT_REQUIREMENTS_NOT_MET/);
});

test('sibling queues remain separate, but a later alias must recheck their real graph and holds',async()=>{
  const e=env();await migrate(e);const fixtures=actualLineageFixtures(),saved=await post(e,await lineage(fixtures.siblings));assert.equal(saved.status,200,await saved.clone().text());
  const a=packet('bird-issue',1,'2026-10-10',['bird',...Array.from({length:9},(_,i)=>'bird-peer-'+i)]),b=packet('cat-issue',2,'2026-10-13',['cat',...Array.from({length:9},(_,i)=>'cat-peer-'+i)]);
  for(const p of [a,b])await stage(e,p);assert.equal((await post(e,activation(a))).status,200);const second=await post(e,activation(b,'2026-10-13T03:30:00+00:00'));assert.equal(second.status,200,await second.clone().text());
  const merged=await post(e,await aliases(fixtures.alias_siblings));assert.equal(merged.status,409);assert.deepEqual((await merged.json()).affected_issue_ids,['bird-issue','cat-issue']);
  const allowed=await post(e,await aliases(fixtures.alias_siblings,[withdrawal(b)]));assert.equal(allowed.status,200,await allowed.clone().text());
});

test('a queued descendant blocks later parent selection and exposure only flows from parent to child',async()=>{
  const e=env();await migrate(e);assert.equal((await post(e,await lineage(actualLineageFixtures().one))).status,200);
  const child=packet('child-first',1,'2026-10-10',['bird',...Array.from({length:9},(_,i)=>'child-first-peer-'+i)]);await stage(e,child);assert.equal((await post(e,activation(child))).status,200);
  const parent=packet('parent-next',2,'2026-10-13',['bear',...Array.from({length:9},(_,i)=>'parent-next-peer-'+i)]);await stage(e,parent);const held=await post(e,activation(parent,'2026-10-13T03:30:00+00:00'));assert.equal(held.status,409);
  await post(e,{kind:'reconcile',client_id:CLIENT},undefined,AFTER);const laterParent=await post(e,activation(parent,'2026-10-13T03:30:00+00:00'),undefined,AFTER);assert.equal(laterParent.status,200,await laterParent.clone().text());
  const other=env();await migrate(other);const p=packet('past-parent',1,'2026-10-10',parent.manifest.concept_ids);await stage(other,p);await post(other,activation(p));await post(other,{kind:'reconcile',client_id:CLIENT},undefined,AFTER);assert.equal((await post(other,await lineage(actualLineageFixtures().one),undefined,AFTER)).status,200);
  const c=packet('new-child',2,'2026-10-13',child.manifest.concept_ids);await stage(other,c);const notNew=await post(other,activation(c,'2026-10-13T03:30:00+00:00'),undefined,AFTER);assert.equal(notNew.status,409);assert.match(await notNew.text(),/FACT_REQUIREMENTS_NOT_MET/);
});

test('actual reviewed reverse edge or alias cannot collapse an existing directional cycle',async()=>{
  const e=env();await migrate(e);await post(e,await lineage(actualLineageFixtures().one));const raw=e.REPORTS.objects.get(ROOT).bytes.slice();
  const reversed=await post(e,await lineage(actualLineageFixtures().reverse));assert.equal(reversed.status,409);assert.match(await reversed.text(),/LINEAGE_CYCLE/);assert.deepEqual(e.REPORTS.objects.get(ROOT).bytes,raw);
  const collapsed=await post(e,await aliases(actualAliasFixtures().exact));assert.equal(collapsed.status,409);assert.match(await collapsed.text(),/LINEAGE_CYCLE/);assert.deepEqual(e.REPORTS.objects.get(ROOT).bytes,raw);
});

test('interrupted lineage staging grants no authority, and lost root reply recovers the original without another write',async()=>{
  const e=env();await migrate(e);const old=e.REPORTS.objects.get(ROOT).bytes.slice(),proof=actualLineageFixtures().one,mutation=await lineage(proof),put=e.REPORTS.put.bind(e.REPORTS);let interrupted=false;
  e.REPORTS.put=async(key,...args)=>{const result=await put(key,...args);if(!interrupted&&key.includes('/lineage-records/')){interrupted=true;throw new Error('Synthetic interrupted original reply');}return result;};
  const failed=await post(e,mutation);assert.equal(failed.status,503);assert.deepEqual(e.REPORTS.objects.get(ROOT).bytes,old);
  const inactive=await handleAdminRequest(req('/admin/index-page?kind=lineage'),e,ctx(),clock(NOW));assert.deepEqual((await inactive.json()).rows,[]);
  e.REPORTS.put=put;const previous=e.REPORTS.objects.get(ROOT).etag;e.REPORTS.loseRootReply=true;
  const lost=await post(e,mutation);assert.equal(lost.status,409);assert.equal((await readRoot(e)).pages.lineage.count,2);
  e.REPORTS.calls.length=0;const recovered=await post(e,mutation,previous);assert.equal(recovered.status,200,await recovered.clone().text());assert.equal((await recovered.json()).replay,true);assert.ok(!e.REPORTS.calls.some(([op])=>op==='put'));
});

test('a later genuine edge cannot normalize or erase duplicated conflicting original references',async()=>{
  const e=env();await migrate(e);await post(e,await lineage(actualLineageFixtures().one));const root=await readRoot(e);root.generation++;root.previous_root_hash=await releaseDigest(await readRoot(e));
  const tree=new ReleaseTree(e.REPORTS,root),key=await lineageKey('bear','bird'),old=await tree.get('lineage',key);old.record_refs.push({...old.record_refs[0],hash:'f'.repeat(64)});
  await tree.set('lineage',key,old);await tree.set('lineage',await lineageKey('bear','bird',1),old);await tree.persist();await e.REPORTS.put(ROOT,releaseBytes(root));
  const before=e.REPORTS.objects.get(ROOT).bytes.slice(),result=await post(e,await lineage(actualLineageFixtures().extra));assert.equal(result.status,409);assert.match(await result.text(),/LINEAGE_PROJECTION_INVALID/);assert.deepEqual(e.REPORTS.objects.get(ROOT).bytes,before);
});

test('actual reviewed alias records activate only with exact owner proof and replay without new writes',async()=>{
  const e=env();assert.equal((await migrate(e)).status,200);const before=e.REPORTS.objects.get(ROOT).etag,proof=actualAliasFixtures().model,mutation=await aliases(proof);
  const denied=await post(e,{...mutation,approval:{...mutation.approval,proof_hash:'f'.repeat(64)}});assert.equal(denied.status,403);
  const saved=await post(e,mutation);assert.equal(saved.status,200,await saved.clone().text());
  const result=await saved.json();assert.equal(result.mutation_applied,true);
  const read=await handleAdminRequest(req('/admin/index-page?kind=aliases&id=bird'),e,ctx(),clock(NOW));
  assert.deepEqual((await read.json()).value.member_ids,['bear','bird']);
  const original=[...e.REPORTS.objects.keys()].find(key=>key.includes('/alias-records/'));assert.ok(original);
  const originalId=original.split('/').at(-1),readOriginal=await handleAdminRequest(req('/admin/alias-records/'+originalId),e,ctx(),clock(NOW));
  assert.equal(readOriginal.status,200);assert.deepEqual(new Uint8Array(await readOriginal.arrayBuffer()),e.REPORTS.objects.get(original).bytes);
  e.REPORTS.calls.length=0;const replay=await post(e,mutation,before);assert.equal(replay.status,200,await replay.clone().text());assert.equal((await replay.json()).replay,true);assert.ok(!e.REPORTS.calls.some(([op])=>op==='put'));
  e.REPORTS.objects.get(original).bytes=enc.encode('{}');const damaged=await post(e,mutation,before);assert.equal(damaged.status,409);assert.match(await damaged.text(),/ALIAS_ORIGINAL_CHANGED/);
});

test('aliases never silently withdraw conflicting queued reports; exact companion approval resolves them',async()=>{
  const e=env();await migrate(e);const p=packet('first'),q=packet('second',2,'2026-10-13');const a=packet('first',1,'2026-10-10',['bear',...p.manifest.concept_ids.slice(1)]),b=packet('second',2,'2026-10-13',['bird',...q.manifest.concept_ids.slice(1)]);
  await stage(e,a);await stage(e,b);assert.equal((await post(e,activation(a))).status,200);assert.equal((await post(e,activation(b,'2026-10-13T03:30:00+00:00'))).status,200);
  const proof=actualAliasFixtures().exact,mutation=await aliases(proof),raw=e.REPORTS.objects.get(ROOT).bytes.slice();
  const blocked=await post(e,mutation);assert.equal(blocked.status,409);const error=await blocked.json();assert.deepEqual(error.affected_issue_ids,['first','second']);assert.deepEqual(e.REPORTS.objects.get(ROOT).bytes,raw);
  const companion=withdrawal(b),bad=await post(e,await aliases(proof,[{...companion,approval:{...companion.approval,approved:false}}]));assert.equal(bad.status,403);assert.deepEqual(e.REPORTS.objects.get(ROOT).bytes,raw);
  const applied=await post(e,await aliases(proof,[companion]));assert.equal(applied.status,200,await applied.clone().text());
  const issue=await handleAdminRequest(req('/admin/index-page?kind=issues&id=second'),e,ctx(),clock(NOW));assert.equal((await issue.json()).value.active_sequence,null);
});

test('new alias cannot reset past exposure or promote a future New hold',async()=>{
  const e=env();await migrate(e);const a=packet('past',1,'2026-10-10',['bear',...Array.from({length:9},(_,i)=>'past-peer-'+i)]);await stage(e,a);assert.equal((await post(e,activation(a))).status,200);assert.equal((await post(e,{kind:'reconcile',client_id:CLIENT},undefined,AFTER)).status,200);
  const b=packet('future',2,'2026-10-13',['bird',...Array.from({length:9},(_,i)=>'future-peer-'+i)]);await stage(e,b);assert.equal((await post(e,activation(b,'2026-10-13T03:30:00+00:00'),undefined,AFTER)).status,200);
  const proof=actualAliasFixtures().exact,blocked=await post(e,await aliases(proof),undefined,AFTER);assert.equal(blocked.status,409);assert.deepEqual((await blocked.json()).affected_issue_ids,['future']);
  const applied=await post(e,await aliases(proof,[withdrawal(b)]),undefined,AFTER);assert.equal(applied.status,200,await applied.clone().text());
  const c=packet('later',2,'2026-10-15',b.manifest.concept_ids);await stage(e,c);const refused=await post(e,activation(c,'2026-10-15T03:30:00+00:00'),undefined,AFTER);assert.equal(refused.status,409);assert.match(await refused.text(),/FACT_REQUIREMENTS_NOT_MET/);
});

test('actual current negative comparison rejects alias activation without altering the current root',async()=>{
  const e=env();await migrate(e);const before=e.REPORTS.objects.get(ROOT).bytes.slice();const result=await post(e,await aliases(actualAliasFixtures().negative));
  assert.equal(result.status,409);assert.match(await result.text(),/ALIAS_CONTRADICTORY_REVIEW/);assert.deepEqual(e.REPORTS.objects.get(ROOT).bytes,before);
});

test('crossing release time while staging cannot backdate a never-exposed withdrawal',async()=>{
  const e=env();await migrate(e);const p=packet();await stage(e,p);assert.equal((await post(e,activation(p))).status,200);
  const original=e.REPORTS.objects.get(ROOT).bytes.slice(),etag=e.REPORTS.objects.get(ROOT).etag;let crossed=false;
  const put=e.REPORTS.put.bind(e.REPORTS);e.REPORTS.put=async(key,...args)=>{const result=await put(key,...args);if(key.includes('/pages/'))crossed=true;return result;};
  const result=await handleAdminRequest(req('/admin/index',{contract:'release-index-v2',expected_etag:etag,mutation:withdrawal(p)}),e,ctx(),{clock:()=>new Date(crossed?AFTER:NOW)});
  assert.equal(result.status,409);const error=await result.json();assert.equal(error.error,'ADMIN_RELEASE_AVAILABILITY_CHANGED');assert.deepEqual(error.affected_issue_ids,['one']);assert.deepEqual(e.REPORTS.objects.get(ROOT).bytes,original);
  assert.equal((await post(e,{kind:'reconcile',client_id:CLIENT},undefined,AFTER)).status,200);
  assert.equal((await post(e,withdrawal(p),undefined,AFTER)).status,200);
  const type=await handleAdminRequest(req('/admin/index-page?kind=types&id=one-type-0'),e,ctx(),clock(AFTER));assert.ok((await type.json()).value.first_exposed_at);
});

test('explicit empty migration switches the same root and fences legacy writers', async () => {
  const e = env(), result = await migrate(e); assert.equal(result.status, 200); const snapshot = await result.json();
  assert.equal(snapshot.index.schema_version, 2); assert.equal(snapshot.index.client_id, CLIENT); assert.equal(snapshot.mutation_applied, true);
  const legacy = await handleAdminRequest(req('/admin/index', { expected_etag: snapshot.etag, mutation: { kind: 'withdraw' } }), e, ctx(), clock(NOW));
  assert.equal(legacy.status, 409); assert.match(await legacy.text(), /LEGACY_WRITER_FENCED/);
  const replay = await post(e, { kind: 'migrate', client_id: CLIENT, finish: true }, null); assert.equal((await replay.json()).replay, true);
});

test('v2 activation, timed downloads, reconciliation and withdrawal preserve permanent exposure', async () => {
  const e = env(); assert.equal((await migrate(e)).status, 200); const p = packet(); await stage(e, p);
  const activated = await post(e, activation(p)); assert.equal(activated.status, 200, await activated.clone().text());
  const first = await activated.json(); assert.equal(first.index.next_sequence, 2); assert.equal(first.index.settled_publication_count, 0);
  const guessed = await client(e, '/reports/download/one/1/report-en.pdf', NOW); assert.equal(guessed.status, 404);
  const downloaded = await client(e, '/reports/download/one/1/report-en.pdf'); assert.equal(downloaded.status, 200); assert.match(await downloaded.text(), /^%PDF/);
  assert.match(await (await client(e, '/reports')).text(), /Product picks/);
  const reconciled = await post(e, { kind: 'reconcile', client_id: CLIENT }, undefined, AFTER); assert.equal(reconciled.status, 200, await reconciled.clone().text());
  const current = await reconciled.json(); assert.equal(current.index.settled_publication_count, 1); assert.equal(current.index.pages.holds, null);
  const mutation = { kind: 'withdraw', client_id: CLIENT, issue_id: 'one', revision: 1, package_hash: p.manifest.package_hash,
    approval: { approved: true, owner_id: 'owner', client_id: CLIENT, issue_id: 'one', revision: 1, package_hash: p.manifest.package_hash, action: 'withdraw' } };
  const withdrawn = await post(e, mutation, undefined, AFTER); assert.equal(withdrawn.status, 200, await withdrawn.clone().text());
  const oldRevision = await handleAdminRequest(req('/admin/index-page?kind=revisions&id=one&revision=1'), e, ctx(), clock(AFTER));
  assert.equal(oldRevision.status, 200); const oldValue = (await oldRevision.json()).value; assert.equal(oldValue.remote_state, 'withdrawn'); assert.equal(oldValue.sequence, 1);
  const absent = await handleAdminRequest(req('/admin/index-page?kind=revisions&id=one&revision=2'), e, ctx(), clock(AFTER)); assert.equal((await absent.json()).value, null);
  e.REPORTS.calls.length = 0; assert.equal((await client(e, '/download/one/1/report-en.pdf')).status, 404);
  assert.ok(!e.REPORTS.calls.some(([op, key]) => key.endsWith('report-en.pdf')));
  const other = packet('other', 2, '2026-10-13', p.manifest.concept_ids); await stage(e, other);
  const refused = await post(e, activation(other, '2026-10-13T03:30:00+00:00'), undefined, AFTER); assert.equal(refused.status, 409);
  assert.match(await refused.text(), /FACT_REQUIREMENTS_NOT_MET/);
});

test('migration staging interruption leaves v1 authoritative and resumes the same immutable pages', async () => {
  const e = env(), p = packet(), activated = await handleAdminRequest(req('/admin/index', { expected_etag: null, mutation: activation(p) }), e, ctx(), clock(NOW));
  // Stage real packet objects before the existing v1 activation route.
  assert.equal(activated.status, 503);
  await stage(e, p); const saved = await handleAdminRequest(req('/admin/index', { expected_etag: null, mutation: activation(p) }), e, ctx(), clock(NOW)); assert.equal(saved.status, 200);
  const before = e.REPORTS.objects.get(ROOT), original = before.bytes.slice(); e.REPORTS.failStage = true;
  assert.equal((await migrate(e)).status, 503); assert.deepEqual(e.REPORTS.objects.get(ROOT).bytes, original);
  const orphan = new Map([...e.REPORTS.objects].filter(([key]) => key.includes('/pages/')));
  e.REPORTS.failStage = false; const resumed = await migrate(e); assert.equal(resumed.status, 200, await resumed.clone().text());
  assert.equal((await resumed.json()).index.schema_version, 2);
  for (const [key, row] of orphan) assert.deepEqual(e.REPORTS.objects.get(key).bytes, row.bytes);
});

test('a concurrent legacy mutation wins over the old migration source ETag', async () => {
  const e = env(), p = packet(); await stage(e, p);
  const activated = await handleAdminRequest(req('/admin/index', { expected_etag: null, mutation: activation(p) }), e, ctx(), clock(NOW)); assert.equal(activated.status, 200);
  const before = e.REPORTS.objects.get(ROOT).etag; assert.equal((await migrate(e, false)).status, 200);
  const changed = await readRoot(e); changed.issues[0].remote_state = 'withdrawn'; changed.issues[0].server_deactivated_at = NOW;
  await e.REPORTS.put(ROOT, bytes(changed));
  const conflict = await post(e, { kind: 'migrate', client_id: CLIENT, finish: true }, before); assert.equal(conflict.status, 409);
  assert.equal((await readRoot(e)).schema_version, 1); assert.equal((await readRoot(e)).issues[0].remote_state, 'withdrawn');
});

test('client cannot read owner root/page APIs or mutate history, and unsigned claims do not touch storage', async () => {
  const e = env(); await migrate(e); e.REPORTS.calls.length = 0;
  const denied = await handleAdminRequest(req('/admin/index-page?kind=archive'), e, ctx('client@example.com'), clock(NOW)); assert.equal(denied.status, 403); assert.equal(e.REPORTS.calls.length, 0);
  const unsigned = await handleRequest(new Request('https://private.example.com/reports/archive', { headers: { 'Cf-Access-Authenticated-User-Email': 'client@example.com' } }), e, {}, Date.parse(AFTER));
  assert.equal(unsigned.status, 403); assert.equal(e.REPORTS.calls.length, 0);
});

test('a lost activation reply reconciles exact stored original before rejecting its old ETag', async () => {
  const e = env(); await migrate(e); const originalEtag = e.REPORTS.objects.get(ROOT).etag, p = packet(); await stage(e, p);
  e.REPORTS.loseRootReply = true; const uncertain = await post(e, activation(p), originalEtag); assert.equal(uncertain.status, 409);
  const saved = e.REPORTS.objects.get(ROOT), originalBytes = saved.bytes.slice(); e.REPORTS.calls.length = 0;
  const replay = await post(e, activation(p), originalEtag, '2026-11-01T10:00:00.000Z'); assert.equal(replay.status, 200, await replay.clone().text());
  const receipt = await replay.json(); assert.equal(receipt.replay, true); assert.equal(receipt.original_server_activated_at, NOW);
  assert.deepEqual(e.REPORTS.objects.get(ROOT).bytes, originalBytes); assert.ok(!e.REPORTS.calls.some(([kind]) => kind === 'put'));
  const altered = activation(p); altered.scheduled_at = '2026-10-13T03:30:00+00:00';
  const denied = await post(e, altered, originalEtag); assert.equal(denied.status, 409);
});

test('queue changes require exact owner-approved dependent withdrawals rather than silent cancellation', async () => {
  const e = env(); await migrate(e); const a = packet('a', 1), b = packet('b', 2, '2026-10-13'); await stage(e, a); await stage(e, b);
  assert.equal((await post(e, activation(a))).status, 200);
  assert.equal((await post(e, activation(b, '2026-10-13T03:30:00+00:00'))).status, 200);
  const withdraw = p => ({ kind: 'withdraw', client_id: CLIENT, issue_id: p.manifest.issue_id, revision: 1, package_hash: p.manifest.package_hash,
    approval: { approved: true, owner_id: 'owner', client_id: CLIENT, issue_id: p.manifest.issue_id, revision: 1, package_hash: p.manifest.package_hash, action: 'withdraw' } });
  const unchanged = e.REPORTS.objects.get(ROOT).bytes.slice(), denied = await post(e, withdraw(a)); assert.equal(denied.status, 409);
  const action = await denied.json(); assert.deepEqual(action.affected_issue_ids, ['b']); assert.equal(action.next_available_at, '2026-10-13T03:30:00.000Z');
  assert.deepEqual(e.REPORTS.objects.get(ROOT).bytes, unchanged);
  const invalid = withdraw(a); invalid.dependent_withdrawals = [withdraw(b)]; invalid.dependent_withdrawals[0].approval.package_hash = '0'.repeat(64);
  assert.equal((await post(e, invalid)).status, 403); assert.deepEqual(e.REPORTS.objects.get(ROOT).bytes, unchanged);
  const approved = withdraw(a); approved.dependent_withdrawals = [withdraw(b)]; const done = await post(e, approved); assert.equal(done.status, 200, await done.clone().text());
  const root = await readRoot(e); assert.equal(root.pages.queue, null); assert.equal(root.pages.holds, null); assert.equal(root.settled_publication_count, 0);
});

test('2,000 reports render in bounded pages; changed root invalidates old cursor without exposing old authority', async () => {
  const e = env(), entries = [];
  for (let i = 1; i <= 2000; i++) { const p = packet('report-' + i, i), m = p.manifest;
    entries.push({ issue_id: m.issue_id, revision: 1, package_hash: m.package_hash, selection_hash: m.selection_hash, facts_hash: m.facts_hash,
      publication_ordinal: i, product_count: 10, new_count: 10, languages: m.languages, files: m.files, title: 'Report ' + i, summary: '',
      remote_state: 'active', approved: true, complete: true, scheduled_at: new Date(Date.UTC(2019, 0, i, 3, 30)).toISOString(),
      server_activated_at: '2018-12-31T10:00:00.000Z', concept_ids: m.concept_ids }); }
  const planned = await buildReleaseHistory(entries, { client: CLIENT, asOf: NOW }); planned.tree.bucket = e.REPORTS; await planned.tree.persist(); await e.REPORTS.put(ROOT, releaseBytes(planned.root));
  e.REPORTS.calls.length = 0; const first = await client(e, '/reports/archive'), html = await first.text(); assert.equal(first.status, 200); assert.equal((html.match(/<article/g) ?? []).length, 20);
  assert.ok(e.REPORTS.calls.length <= 160); assert.match(html, /Report 2000/); assert.doesNotMatch(html, /Report 1980<\/h/);
  const token = /\?cursor=([^"<]+)/.exec(html)[1], next = await client(e, '/reports/archive?cursor=' + token); assert.equal(next.status, 200); assert.match(await next.text(), /Report 1980/);
  const root = await readRoot(e); root.generation++; root.previous_root_hash = await releaseDigest(planned.root); await e.REPORTS.put(ROOT, releaseBytes(root));
  const stale = await client(e, '/reports/archive?cursor=' + token); assert.equal(stale.status, 409); assert.match(await stale.text(), /list changed/);
  const owner = await handleAdminRequest(req('/admin/index-page?kind=archive'), e, ctx(), clock(NOW)); assert.equal(owner.status, 200); assert.equal((await owner.json()).rows.length, 20);
  const status = await handleAdminStatus(new Request('https://private.example.com/admin'), e, ctx(), 'data:image/png;base64,AA==', clock(NOW));
  assert.equal(status.status, 200); const statusHtml = await status.text(); assert.match(statusHtml, /Saved report versions<\/th><td>2000/);
  assert.match(statusHtml, /Versions checked on this page<\/th><td>20/); assert.match(statusHtml, /status counts below cover only/);
});

test('multi-step migration keeps its source root until every bounded immutable group is verified', async () => {
  const e = env(), entries = [];
  for (let i = 1; i <= 60; i++) { const p = packet('migration-' + i, i), m = p.manifest;
    entries.push({ issue_id: m.issue_id, revision: 1, package_hash: m.package_hash, selection_hash: m.selection_hash, facts_hash: m.facts_hash,
      publication_ordinal: i, product_count: 10, new_count: 10, languages: m.languages, files: m.files, title: 'Migration ' + i,
      remote_state: 'active', approved: true, complete: true, scheduled_at: '2026-09-01T03:30:00.000Z',
      server_activated_at: '2026-08-31T10:00:00.000Z', concept_ids: m.concept_ids }); }
  const v1 = { schema_version: 1, client_id: CLIENT, issues: entries }; validateReleaseIndex(v1, CLIENT, NOW); await e.REPORTS.put(ROOT, bytes(v1));
  const original = e.REPORTS.objects.get(ROOT), source = original.bytes.slice(); let turns = 0, complete = false;
  while (!complete && ++turns <= 100) {
    e.REPORTS.calls.length = 0; const response = await post(e, { kind: 'migrate', client_id: CLIENT, finish: true }, original.etag);
    assert.equal(response.status, 200, await response.clone().text()); const result = await response.json();
    assert.ok(e.REPORTS.calls.length < 55);
    complete = result.mutation_applied;
    if (!complete) { assert.equal(result.index.schema_version, 1); assert.deepEqual(e.REPORTS.objects.get(ROOT).bytes, source); assert.ok(result.migration.next_object < result.migration.object_count); }
    else { assert.equal(result.index.schema_version, 2); assert.equal(result.index.settled_publication_count, 60); assert.equal(result.index.pages.archive.count, 60); assert.equal(result.index.pages.revisions.count, 60); }
  }
  assert.equal(complete, true); assert.ok(turns > 1);
});
