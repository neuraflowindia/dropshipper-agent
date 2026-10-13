// Manufactured packet bytes and fake private R2 only. No hosted proof claim.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { canonical,handleAdminRequest } from './release-admin.mjs';
import { handleRequest } from './worker.mjs';

const enc = new TextEncoder(), sha = bytes => createHash('sha256').update(bytes).digest('hex');
const encoded = value => enc.encode(canonical(value) + '\n');
const CLIENT = 'client-one', PREFIX = `clients/${CLIENT}/issues/fixture-issue/r1/`;
const context = (email = 'owner@example.com', aud = 'owner-aud') => ({ access: { aud, getIdentity: async () => ({ email }) } });
const request = (path, method = 'GET') => new Request('https://reports.example.com' + path, { method });

function environment() {
  const objects = new Map(), calls = [];
  const files = ['shortlist.xlsx', 'report-en.pdf', 'report-hi-Latn.pdf'].map(name => {
    const bytes = enc.encode(name.endsWith('.pdf') ? '%PDF-1.7\nFictional private fixture' : 'PK fictional workbook');
    objects.set(PREFIX + name, bytes);
    return { name, key: PREFIX + name, size: bytes.length, sha256: sha(bytes),
      media_type: name.endsWith('.pdf') ? 'application/pdf' : 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' };
  });
  const facts = encoded({ fictional_fixture: true });
  objects.set(PREFIX + 'final_report_data.json', facts);
  const manifest = { schema_version: 1, client_id: CLIENT, issue_id: 'fixture-issue', revision: 1,
    state: 'draft_ready_for_review', evidence_type: 'captured', complete: true, approved: false, release_ready: false,
    publication_ordinal: 1, product_count: 10, new_count: 10, facts_hash: 'b'.repeat(64), languages: ['en', 'hi-Latn'], files,
    artifacts: { facts: { size: facts.length, sha256: sha(facts) } } };
  manifest.package_hash = sha(enc.encode(canonical(manifest)));
  objects.set(PREFIX + 'delivery-manifest.json', encoded(manifest));
  const bucket = {
    async get(key) {
      calls.push(['get', key]);
      const raw = objects.get(key);
      return raw ? { size: raw.length, etag: 'actual-test-etag', arrayBuffer: async () => raw.slice().buffer } : null;
    },
    async list(options) {
      calls.push(['list', options.prefix]);
      return { objects: [...objects.keys()].filter(key => key.startsWith(options.prefix)).sort().map(key => ({ key })), truncated: false };
    },
    async head(key) {
      calls.push(['head', key]);
      const raw = objects.get(key);
      return raw ? { size: raw.length, customMetadata: { sha256: sha(raw) } } : null;
    },
    async put() { throw new Error('Read-only owner pages may never write.'); },
  };
  return { env: { CLIENT_ID: CLIENT, ACCESS_AUD: 'owner-aud', OPERATOR_EMAILS: '["owner@example.com"]',
    ALLOWED_EMAILS: '["client@example.com"]', REPORTS: bucket }, objects, calls, manifest };
}

function manyDrafts(total=31,junk=0){
  const state=environment();
  for(let i=0;i<total-1;i++){
    const issue='draft-'+String(i).padStart(4,'0'),path=`clients/${CLIENT}/issues/${issue}/r1/`,manifest=structuredClone(state.manifest);
    manifest.issue_id=issue;manifest.files=manifest.files.map(file=>{state.objects.set(path+file.name,state.objects.get(file.key).slice());return {...file,key:path+file.name};});
    state.objects.set(path+'final_report_data.json',state.objects.get(PREFIX+'final_report_data.json').slice());delete manifest.package_hash;manifest.package_hash=sha(enc.encode(canonical(manifest)));state.objects.set(path+'delivery-manifest.json',encoded(manifest));
  }
  for(let i=0;i<junk;i++)state.objects.set(`clients/${CLIENT}/issues/000-junk/file-${String(i).padStart(4,'0')}.jpg`,enc.encode('unrelated saved object'));
  state.env.REPORTS.list=async options=>{
    state.calls.push(['list',options.prefix]);const keys=[...state.objects.keys()].filter(key=>key.startsWith(options.prefix)).sort(),start=options.cursor===undefined?0:Number(options.cursor.slice(5));
    assert.ok(Number.isSafeInteger(start)&&start>=0);const page=keys.slice(start,start+options.limit),end=start+page.length;
    return {objects:page.map(key=>({key})),truncated:end<keys.length,...(end<keys.length?{cursor:'page-'+end}:{})};
  };
  return state;
}
async function draftPage(state,cursor=null,ctx=context()){
  return handleAdminRequest(new Request('https://reports.example.com/admin/draft-page'+(cursor===null?'':'?cursor='+encodeURIComponent(cursor)),{headers:{'X-NeuraFlow-Release-Action':'v1'}}),state.env,ctx);
}

test('bounded private draft pages retain every unused candidate and reach older drafts',async()=>{
  const state=manyDrafts(),seen=new Set();let cursor=null,pages=0;
  do{
    state.calls.length=0;const response=await draftPage(state,cursor);assert.equal(response.status,200,await response.clone().text());const result=await response.json();pages++;
    assert.equal(result.contract,'private-draft-list-v2');assert.equal(result.source_prefix,`clients/${CLIENT}/issues/`);assert.ok(result.scanned_versions<=8&&result.source_pages<=3);assert.equal(result.index_write_attempted,false);assert.equal(result.publication_attempted,false);
    assert.equal(result.scanned_versions,result.checked_versions+result.released_versions);assert.ok(state.calls.filter(([kind])=>kind==='head').length<=32);
    for(const draft of result.drafts){assert.ok(!seen.has(draft.issue_id));seen.add(draft.issue_id);}cursor=result.next_cursor;
  }while(cursor&&pages<12);
  assert.equal(cursor,null);assert.equal(seen.size,31);assert.equal(pages,4);
  const html=await handleRequest(request('/admin/drafts'),state.env,context());assert.equal(html.status,200);assert.match(await html.text(),/More saved drafts/);
});

test('bounded empty source pages keep a real continuation instead of hiding later drafts',async()=>{
  const state=manyDrafts(2,400),first=await (await draftPage(state)).json();assert.equal(first.drafts.length,0);assert.equal(first.source_pages,3);assert.ok(first.next_cursor);let cursor=first.next_cursor,seen=[];
  for(let n=0;n<5&&cursor;n++){const response=await draftPage(state,cursor);assert.equal(response.status,200,await response.clone().text());const page=await response.json();seen.push(...page.drafts);cursor=page.next_cursor;}
  assert.equal(cursor,null);assert.equal(seen.length,2);
});

test('changed root/source invalidates only the old draft cursor, with no writes or borrowed prefix',async()=>{
  for(const change of ['source','root']){
    const state=manyDrafts(),first=await (await draftPage(state)).json();assert.ok(first.next_cursor);
    if(change==='source')state.objects.set(`clients/${CLIENT}/issues/000-added/object`,enc.encode('added'));
    else state.objects.set(`clients/${CLIENT}/release-index.json`,encoded({schema_version:1,client_id:CLIENT,issues:[]}));
    const response=await draftPage(state,first.next_cursor);assert.equal(response.status,409);assert.match(await response.text(),/CURSOR_CHANGED/);
  }
  const state=manyDrafts(),first=await (await draftPage(state)).json(),token=JSON.parse(Buffer.from(first.next_cursor,'base64url').toString());token.source_prefix='clients/another-client/issues/';
  const invalid=await draftPage(state,Buffer.from(JSON.stringify(token)).toString('base64url'));assert.equal(invalid.status,400);
  state.calls.length=0;assert.equal((await draftPage(state,first.next_cursor,context('client@example.com'))).status,403);assert.deepEqual(state.calls,[]);
});

test('2,000 private drafts remain reachable in bounded requests without a draft index or writes',async()=>{
  const state=manyDrafts(2000),seen=new Set();let cursor=null,pages=0;
  do{
    state.calls.length=0;const response=await draftPage(state,cursor);assert.equal(response.status,200,await response.clone().text());const page=await response.json();pages++;
    assert.ok(page.source_pages<=3&&page.scanned_versions<=8);assert.ok(state.calls.length<=44);
    for(const draft of page.drafts){assert.ok(!seen.has(draft.issue_id));seen.add(draft.issue_id);}cursor=page.next_cursor;
  }while(cursor&&pages<300);
  assert.equal(cursor,null);assert.equal(seen.size,2000);assert.equal(pages,250);
});

test('private drafts are owner-only, show three file links, and leave the client list empty', async () => {
  const { env, objects, calls } = environment();
  const result = await handleRequest(request('/admin/drafts'), env, context());
  assert.equal(result.status, 200);
  const html = await result.text();
  assert.match(html, /10 products\. Not approved for the client/);
  for (const name of ['shortlist.xlsx', 'report-en.pdf', 'report-hi-Latn.pdf']) assert.ok(html.includes('/admin/download/fixture-issue/1/' + name));
  assert.match(html, /have not been released to the client/);
  assert.ok(!objects.has(`clients/${CLIENT}/release-index.json`));
  assert.ok(!calls.some(([kind]) => kind === 'put'));
});

test('unauthorised identities cannot list or download even with a guessed path or HEAD', async () => {
  for (const ctx of [{}, context('client@example.com'), context('owner@example.com', 'wrong-aud')]) {
    for (const path of ['/admin/drafts', '/admin/download/fixture-issue/1/report-en.pdf']) {
      const { env, calls } = environment();
      assert.equal((await handleRequest(request(path, 'HEAD'), env, ctx)).status, 403);
      assert.deepEqual(calls, []);
    }
  }
});

test('owner download verifies real full bytes and returns an attached private PDF', async () => {
  const { env, objects } = environment();
  const response = await handleRequest(request('/admin/download/fixture-issue/1/report-en.pdf'), env, context());
  assert.equal(response.status, 200);
  assert.equal(response.headers.get('Content-Disposition'), 'attachment; filename="report-en.pdf"');
  assert.match(response.headers.get('Cache-Control'), /private.*no-store/);
  assert.deepEqual(new Uint8Array(await response.arrayBuffer()), objects.get(PREFIX + 'report-en.pdf'));
  const head = await handleRequest(request('/admin/download/fixture-issue/1/report-en.pdf', 'HEAD'), env, context());
  assert.equal(head.status, 200); assert.equal(await head.text(), '');
});

test('changed bytes, wrong manifest hash, missing facts, and incomplete files never become downloadable confirmations', async () => {
  for (const alter of [
    state => state.objects.set(PREFIX + 'report-en.pdf', enc.encode('changed')),
    state => state.objects.set(PREFIX + 'delivery-manifest.json', encoded({ ...state.manifest, product_count: 11 })),
    state => state.objects.delete(PREFIX + 'final_report_data.json'),
  ]) {
    const state = environment(); alter(state);
    const response = await handleRequest(request('/admin/drafts'), state.env, context());
    assert.equal(response.status, 200);
    const html = await response.text();
    assert.doesNotMatch(html, /href="\/admin\/download\//);
    assert.match(html, /could not be checked/);
  }
  const state = environment(); state.objects.set(PREFIX + 'report-en.pdf', enc.encode('changed'));
  assert.equal((await handleRequest(request('/admin/download/fixture-issue/1/report-en.pdf'), state.env, context())).status, 409);
});

test('a failed saved version does not hide another healthy draft', async () => {
  const state = environment();
  state.objects.set(`clients/${CLIENT}/issues/zz-bad/r1/delivery-manifest.json`, enc.encode('not valid JSON'));
  const result = await handleRequest(request('/admin/drafts'), state.env, context());
  assert.equal(result.status, 200);
  const text = await result.text();
  assert.match(text, /fixture-issue/); assert.match(text, /1 saved draft versions could not be checked/);
});

test('a repeated or invalid pagination cursor is a visible list failure, not a loop', async () => {
  const state = environment(); let calls = 0;
  state.env.REPORTS.list = async () => { calls++; return { objects: [], truncated: true, cursor: 'same-cursor' }; };
  assert.equal((await handleRequest(request('/admin/drafts'), state.env, context())).status, 503);
  assert.equal(calls, 2);
});

test('methods and search options cannot turn owner downloads or lists into writes', async () => {
  for (const path of ['/admin/drafts', '/admin/download/fixture-issue/1/report-en.pdf']) {
    const state = environment();
    assert.equal((await handleRequest(request(path, 'POST'), state.env, context())).status, 405);
    assert.ok(!state.calls.some(([kind]) => kind === 'list' || kind === 'head'));
  }
  const state = environment();
  assert.equal((await handleRequest(request('/admin/download/fixture-issue/1/report-en.pdf?bypass=yes'), state.env, context())).status, 404);
  assert.equal((await handleRequest(request('/admin/drafts?bypass=yes'), state.env, context())).status, 400);
});

test('the client report routes cannot access staged owner drafts before approval', async () => {
  const state = environment();
  const response = await handleRequest(request('/reports/download/fixture-issue/1/report-en.pdf'), state.env, context('client@example.com'));
  assert.equal(response.status, 404);
  assert.ok(!state.calls.some(([kind, key]) => kind === 'get' && key.endsWith('report-en.pdf')));
});
