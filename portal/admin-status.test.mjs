// Manufactured index and fake private storage only. No hosted acceptance claim.
import test from 'node:test';
import assert from 'node:assert/strict';
import { handleAdminStatus } from './admin-status.mjs';
import { handleRequest } from './worker.mjs';

const NOW = '2026-10-13T04:00:00.000Z';
const CLIENT = 'client-one';
const INDEX = `clients/${CLIENT}/release-index.json`;
const context = (email = 'owner@example.com', aud = 'owner-worker-aud') => ({ access: { aud, getIdentity: async () => ({ email }) } });
const request = (path = '/admin', method = 'GET', headers = {}) => new Request('https://dropshipper.example.com' + path, { method, headers });

function issue(id, changes = {}) {
  const value = { issue_id: id, revision: 1, title: `Report ${id}`, remote_state: 'active', approved: true, complete: true,
    scheduled_at: '2026-10-13T03:30:00Z', server_activated_at: '2026-10-12T09:00:00Z',
    package_hash: 'a'.repeat(64), selection_hash: 'b'.repeat(64), publication_ordinal: 1,
    product_count: 10, new_count: 8, languages: ['en', 'hi-Latn'], ...changes };
  value.files ??= ['shortlist.xlsx', ...value.languages.map(language => `report-${language}.pdf`)].map(name => ({
    name, key: `clients/${CLIENT}/issues/${id}/r1/${name}`, size: 12, sha256: 'c'.repeat(64),
    media_type: name.endsWith('.pdf') ? 'application/pdf' : 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' }));
  return value;
}

function environment(issues = null) {
  const calls = [];
  const index = issues === null ? null : { schema_version: 1, client_id: CLIENT, issues };
  const bytes = new TextEncoder().encode(JSON.stringify(index));
  const env = { CLIENT_ID: CLIENT, ACCESS_AUD: 'owner-worker-aud', OPERATOR_EMAILS: '["owner@example.com"]', ALLOWED_EMAILS: '[]', REPORTS: {
    async get(key) {
      calls.push(['get', key]);
      assert.equal(key, INDEX); // Never fetch files or other accounts.
      return index === null ? null : { etag: 'actual-fixture-etag', size: bytes.byteLength, arrayBuffer: async () => bytes.slice().buffer };
    },
    async put() { calls.push(['put']); throw new Error('A read-only page must never write.'); },
  } };
  return { env, calls };
}

const render = (env, ctx = context(), req = request()) => handleAdminStatus(req, env, ctx, 'data:image/png;base64,fixture', { clock: () => new Date(NOW) });

test('owner status is reachable at /admin and /admin/ without the API action header', async () => {
  for (const route of ['/admin', '/admin/']) {
    const { env, calls } = environment();
    const result = await handleRequest(request(route), env, context());
    assert.equal(result.status, 200);
    assert.match(result.headers.get('Content-Type'), /text\/html/);
    assert.match(await result.text(), /No report list is saved on this website yet/);
    assert.deepEqual(calls, [['get', INDEX]]);
  }
});

test('signed out, clients, unknown owner and forged email/JWT headers cannot read owner data', async () => {
  for (const ctx of [{}, context('client@example.com'), context('another@gmail.com'), context('owner@example.com', 'wrong-aud'),
    { access: { aud: 'owner-worker-aud', getIdentity: async () => { throw new Error('private identity error'); } } },
    { access: { aud: 'owner-worker-aud', getIdentity: async () => null } }]) {
    const { env, calls } = environment([issue('private')]);
    env.ALLOWED_EMAILS = '["client@example.com"]';
    const result = await render(env, ctx, request('/admin', 'GET', {
      'CF-Access-Authenticated-User-Email': 'owner@example.com', 'Cf-Access-Jwt-Assertion': 'invented-token' }));
    assert.equal(result.status, 403);
    assert.equal(await result.text(), 'Owner access required.');
    assert.deepEqual(calls, []);
  }
});

test('configuration errors and overlapping owner/client lists fail closed before storage', async () => {
  for (const change of [env => { env.ALLOWED_EMAILS = '["owner@example.com"]'; }, env => { env.OPERATOR_EMAILS = '[]'; },
    env => { env.ACCESS_AUD = ''; }, env => { env.CLIENT_ID = '../other-account'; }, env => { env.ALLOWED_EMAILS = 'bad-json'; },
    env => { env.REPORTS.put = null; }]) {
    const { env, calls } = environment(); change(env);
    const result = await render(env);
    assert.equal(result.status, 503);
    assert.match(await result.text(), /temporarily unavailable/);
    assert.deepEqual(calls, []);
  }
});

test('empty stored index is distinguished from an absent index without creating either', async () => {
  for (const issues of [null, []]) {
    const { env, calls } = environment(issues);
    const html = await (await render(env)).text();
    assert.match(html, issues === null ? /No report list is saved on this website yet/ : /saved report list is empty/);
    assert.match(html, /Saved report versions<\/th><td>0/);
    assert.match(html, /Research and drafts that are not yet listed are not counted here/);
    assert.deepEqual(calls, [['get', INDEX]]);
  }
});

test('real saved entries are counted using the verified server time and retained withdrawal states', async () => {
  const entries = [issue('current'), issue('future', { scheduled_at: '2026-10-15T03:30:00Z' }),
    issue('withdrawn', { remote_state: 'withdrawn', server_deactivated_at: '2026-10-13T03:45:00Z' }),
    issue('pending', { approved: false, complete: false, publication_ordinal: null, languages: [], files: [] })];
  const { env, calls } = environment(entries);
  const response = await render(env);
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /Saved report versions<\/th><td>4/);
  for (const label of ['Available now', 'Scheduled for later', 'Withdrawn', 'Waiting for approval or completion']) {
    assert.ok(html.includes(label + '</th><td>1'), label);
  }
  assert.match(html, /Report current<\/th><td>Available now<\/td><td>10/);
  assert.match(html, /Report future<\/th><td>Scheduled for later/);
  assert.match(html, /India time/);
  assert.deepEqual(calls, [['get', INDEX]]);
});

test('stored title markup is escaped as text and no executable scripts, forms or external assets are added', async () => {
  const title = '<img src=x onerror="alert(1)"> & \'owner\'';
  const { env } = environment([issue('safe', { title })]);
  const response = await render(env);
  const html = await response.text();
  assert.ok(html.includes('&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &amp; &#39;owner&#39;'));
  assert.ok(!html.includes(title));
  assert.doesNotMatch(html, /<script\b|<form\b|<input\b|<button\b|src="https?:/);
  assert.match(html, /href="\/admin">Refresh status/);
  assert.match(html, /cannot upload, edit, approve or publish/);
  assert.match(response.headers.get('Content-Security-Policy'), /default-src 'none'/);
  assert.doesNotMatch(response.headers.get('Content-Security-Policy'), /script-src|unsafe-eval/);
});

test('success, denial and storage failure all retain private no-store and framing protection', async () => {
  const ok = environment(), denied = environment(), failed = environment();
  failed.env.REPORTS.get = async () => { throw new Error('private-provider-secret'); };
  for (const result of [await render(ok.env), await render(denied.env, {}), await render(failed.env)]) {
    assert.match(result.headers.get('Cache-Control'), /private.*no-store/);
    assert.equal(result.headers.get('Vary'), 'Cookie');
    assert.equal(result.headers.get('X-Content-Type-Options'), 'nosniff');
    assert.equal(result.headers.get('Referrer-Policy'), 'no-referrer');
    assert.match(result.headers.get('Content-Security-Policy'), /frame-ancestors 'none'/);
    assert.doesNotMatch(await result.text(), /private-provider-secret/);
  }
});

test('invalid private storage is unavailable, never presented as a successful zero count', async () => {
  for (const stored of [{ size: 3, etag: 'etag', arrayBuffer: async () => new TextEncoder().encode('bad').buffer },
    { size: 1024 * 1024, etag: 'etag', arrayBuffer: async () => new ArrayBuffer(1024 * 1024) },
    { size: 3, arrayBuffer: async () => new TextEncoder().encode('bad').buffer }]) {
    const { env } = environment(); env.REPORTS.get = async () => stored;
    const result = await render(env);
    assert.equal(result.status, 503);
    const text = await result.text();
    assert.match(text, /temporarily unavailable/);
    assert.doesNotMatch(text, /Saved report versions|successfully|reachable/);
  }
});

test('HEAD is owner-protected and bodyless; unsupported methods cannot write', async () => {
  const { env, calls } = environment();
  const head = await render(env, context(), request('/admin', 'HEAD'));
  assert.equal(head.status, 200); assert.equal(await head.text(), '');
  assert.equal((await render(env, {}, request('/admin', 'HEAD'))).status, 403);
  for (const method of ['POST', 'PUT', 'DELETE']) {
    const result = await render(env, context(), request('/admin', method));
    assert.equal(result.status, 405); assert.equal(result.headers.get('Allow'), 'GET, HEAD');
  }
  assert.ok(calls.every(([kind]) => kind === 'get'));
});

test('the original API still requires its action header and rejects cross-origin write requests', async () => {
  for (const req of [request('/admin/index'), request('/admin/index', 'POST'),
    request('/admin/index', 'POST', { 'X-NeuraFlow-Release-Action': 'v1', Origin: 'https://hostile.example.com' }),
    request('/admin/index?bypass=1', 'GET', { 'X-NeuraFlow-Release-Action': 'v1' })]) {
    const { env, calls } = environment();
    const result = await handleRequest(req, env, context());
    assert.equal(result.status, 403);
    assert.equal(await result.text(), 'ADMIN_ACTION_REQUIRED');
    assert.deepEqual(calls, []);
  }
});

test('the new owner page does not grant owners client report access or expose it to clients', async () => {
  const { env, calls } = environment(); env.ALLOWED_EMAILS = '["client@example.com"]';
  assert.equal((await handleRequest(request('/reports'), env, context())).status, 403);
  assert.equal((await handleRequest(request('/admin'), env, context('client@example.com'))).status, 403);
  assert.deepEqual(calls, []);
});
