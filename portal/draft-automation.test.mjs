// Fictional Access service identity and fake private bucket; no provider calls.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { handleRequest } from './worker.mjs';
import { handleDraftAutomationRequest } from './release-admin.mjs';
import { createServiceAssertionVerifier, ServiceAssertionError } from './access-service-assertion.mjs';

const ID = 'a'.repeat(32) + '.access', AUD = 'b'.repeat(64);
const enc = new TextEncoder(), hash = raw => createHash('sha256').update(raw).digest('hex');
const BASE = '/_automation/drafts', KEY = 'clients/client-one/issues/issue-one/r1/report-en.pdf';
const context = (identity = { service_token_id: ID, service_token_status: true }, aud = AUD) => ({ access: { aud, getIdentity: async () => identity } });
function environment() {
  const objects = new Map(), calls = [];
  const env = { CLIENT_ID: 'client-one', ACCESS_AUD: 'owner-aud', OPERATOR_EMAILS: '["owner@example.com"]',
    ALLOWED_EMAILS: '["client@example.com"]', DRAFT_AUTOMATION_AUD: AUD, DRAFT_AUTOMATION_CLIENT_ID: ID,
    REPORTS: {
      async get(key) { calls.push(['get', key]); const raw = objects.get(key); return raw ? { size: raw.length, etag: 'fixture-etag', arrayBuffer: async () => raw.slice().buffer } : null; },
      async put(key, raw, options) { calls.push(['put', key]); assert.equal(options.onlyIf.get('If-None-Match'), '*'); if (objects.has(key)) return null; objects.set(key, new Uint8Array(raw)); return { etag: 'fixture-created' }; },
    } };
  return { env, objects, calls };
}
function request(path, method = 'GET', raw) {
  const headers = { 'X-NeuraFlow-Release-Action': 'v1' };
  if (raw) { headers['Content-Type'] = 'application/pdf'; headers['X-Content-SHA256'] = hash(raw); }
  return new Request('https://reports.example.com' + path, { method, headers, ...(raw ? { body: raw } : {}) });
}

test('machine reads the actual client index without writing it', async () => {
  const state = environment(); const result = await handleRequest(request(BASE + '/index'), state.env, context());
  assert.equal(result.status, 200); const value = await result.json();
  assert.equal(value.index.client_id, 'client-one'); assert.deepEqual(value.index.issues, []);
  assert.deepEqual(state.calls, [['get', 'clients/client-one/release-index.json']]);
});
test('machine immutable upload, exact readback and same-byte replay preserve private bytes', async () => {
  const state = environment(), raw = enc.encode('%PDF-1.7\nFictional draft'), path = BASE + '/objects/issue-one/1/report-en.pdf';
  for (let i = 0; i < 2; i++) {
    const written = await handleRequest(request(path, 'PUT', raw), state.env, context());
    assert.equal(written.status, 200); assert.equal((await written.json()).sha256, hash(raw));
  }
  const result = await handleRequest(request(path), state.env, context());
  assert.equal(result.status, 200); assert.equal(result.headers.get('X-Content-SHA256'), hash(raw));
  assert.deepEqual(new Uint8Array(await result.arrayBuffer()), raw); assert.deepEqual(state.objects.get(KEY), raw);
  assert.ok(!state.objects.has('clients/client-one/release-index.json'));
});
test('missing context, wrong audience, owner email, false or wrong service identity never access storage', async () => {
  for (const ctx of [{}, context(undefined, 'wrong-aud'), context({ email: 'owner@example.com' }),
                     context({ service_token_id: ID, service_token_status: false }), context({ service_token_id: 'other.access', service_token_status: true })]) {
    const state = environment(); const result = await handleRequest(request(BASE + '/index'), state.env, ctx);
    assert.equal(result.status, 403); assert.deepEqual(state.calls, []);
  }
});
test('identity failures give only fixed reasons and never private claim values', async () => {
  const cases = [
    [{}, 'ADMIN_DRAFT_AUTOMATION_CONTEXT_REQUIRED'],
    [context(undefined, 'wrong-aud'), 'ADMIN_DRAFT_AUTOMATION_AUDIENCE_MISMATCH'],
    [{ access: { aud: AUD } }, 'ADMIN_DRAFT_AUTOMATION_IDENTITY_UNAVAILABLE'],
    [{ access: { aud: AUD, getIdentity: async () => { throw new Error('private-provider-claims'); } } }, 'ADMIN_DRAFT_AUTOMATION_IDENTITY_UNAVAILABLE'],
    [context(null), 'ADMIN_DRAFT_AUTOMATION_IDENTITY_MISSING'],
    [{ access: { aud: AUD, getIdentity: async () => undefined } }, 'ADMIN_DRAFT_AUTOMATION_IDENTITY_MISSING'],
    [context('private-provider-claims'), 'ADMIN_DRAFT_AUTOMATION_IDENTITY_INVALID'],
    [context([]), 'ADMIN_DRAFT_AUTOMATION_IDENTITY_INVALID'],
    [context({ email: 'private-owner@example.com' }), 'ADMIN_DRAFT_AUTOMATION_SERVICE_STATUS_MISSING'],
    [context({ service_token_id: ID, service_token_status: 'true' }), 'ADMIN_DRAFT_AUTOMATION_SERVICE_IDENTITY_REQUIRED'],
    [context({ service_token_id: ID, service_token_status: 1 }), 'ADMIN_DRAFT_AUTOMATION_SERVICE_IDENTITY_REQUIRED'],
    [context({ service_token_id: 'private-other-service.access', service_token_status: true }), 'ADMIN_DRAFT_AUTOMATION_SERVICE_ID_MISMATCH'],
  ];
  for (const [ctx, expected] of cases) {
    const state = environment(), result = await handleRequest(request(BASE + '/index'), state.env, ctx);
    assert.equal(result.status, 403); assert.equal(await result.text(), expected); assert.deepEqual(state.calls, []);
  }
});
test('forged service headers without trusted Access context cannot access storage', async () => {
  const state = environment(), req = request(BASE + '/index');
  req.headers.set('CF-Access-Client-Id', ID); req.headers.set('CF-Access-Client-Secret', 'fictional-header');
  assert.equal((await handleRequest(req, state.env, {})).status, 403); assert.deepEqual(state.calls, []);
});
test('machine cannot mutate the release index or access owner/client routes', async () => {
  for (const [path, method] of [[BASE + '/index', 'POST'], [BASE + '/index', 'DELETE'], ['/admin/index', 'GET'],
                              ['/admin/drafts', 'GET'], ['/reports', 'GET'], [BASE + '/approve', 'POST']]) {
    const state = environment(); const result = await handleRequest(request(path, method), state.env, context());
    assert.ok([403, 404, 405].includes(result.status)); assert.deepEqual(state.calls, []);
  }
});
test('only five fixed object names and numeric revisions are accepted', async () => {
  for (const path of [BASE + '/objects/issue-one/1/secret.json', BASE + '/objects/issue-one/0/report-en.pdf',
                      BASE + '/objects/issue-one/9007199254740992/report-en.pdf', BASE + '/objects/other-client/issue-one/1/report-en.pdf']) {
    const state = environment(); assert.equal((await handleRequest(request(path), state.env, context())).status, 404);
    assert.deepEqual(state.calls, []);
  }
});
test('different existing object bytes are never overwritten', async () => {
  const state = environment(), raw = enc.encode('%PDF-1.7\nNew bytes'), original = enc.encode('%PDF-1.7\nOriginal bytes'); state.objects.set(KEY, original);
  const result = await handleRequest(request(BASE + '/objects/issue-one/1/report-en.pdf', 'PUT', raw), state.env, context());
  assert.equal(result.status, 409); assert.deepEqual(state.objects.get(KEY), original);
});
test('actual absent object returns404 while failed or malformed reads remain503', async () => {
  const path = BASE + '/objects/issue-one/1/report-en.pdf', missing = environment();
  assert.equal((await handleRequest(request(path), missing.env, context())).status, 404);
  for (const get of [async () => { throw new Error('read failed'); }, async () => ({ size: -1, arrayBuffer: async () => new ArrayBuffer(0) })]) {
    const state = environment(); state.env.REPORTS.get = get;
    assert.equal((await handleRequest(request(path), state.env, context())).status, 503);
  }
});
test('query strings, unrelated browser origin and absent action marker are refused', async () => {
  for (const mutate of [req => req.headers.set('Origin', 'https://unrelated.example'), req => req.headers.delete('X-NeuraFlow-Release-Action')]) {
    const state = environment(), req = request(BASE + '/index'); mutate(req);
    assert.equal((await handleRequest(req, state.env, context())).status, 403); assert.deepEqual(state.calls, []);
  }
  const state = environment(); assert.equal((await handleRequest(request(BASE + '/index?activate=true'), state.env, context())).status, 403);
  assert.deepEqual(state.calls, []);
});
test('missing machine configuration and failed identity retrieval have no storage effects', async () => {
  const state = environment(); delete state.env.DRAFT_AUTOMATION_AUD;
  assert.equal((await handleRequest(request(BASE + '/index'), state.env, context())).status, 503); assert.deepEqual(state.calls, []);
  const other = environment(), ctx = { access: { aud: AUD, getIdentity: async () => { throw new Error('unavailable'); } } };
  assert.equal((await handleRequest(request(BASE + '/index'), other.env, ctx)).status, 403); assert.deepEqual(other.calls, []);
});

async function signedFixture() {
  const issuer = 'https://fixture-team.cloudflareaccess.com', pair = await crypto.subtle.generateKey({
    name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256',
  }, true, ['sign', 'verify']);
  const jwk = { ...await crypto.subtle.exportKey('jwk', pair.publicKey), kid: 'fixture-service-key', alg: 'RS256', use: 'sig' };
  const now = Math.floor(Date.now() / 1000), claims = { type: 'app', iss: issuer, aud: [AUD],
    iat: now - 5, exp: now + 300, sub: '', common_name: ID };
  const input = Buffer.from(JSON.stringify({ alg: 'RS256', kid: jwk.kid, typ: 'JWT' })).toString('base64url') + '.' +
    Buffer.from(JSON.stringify(claims)).toString('base64url');
  const signature = await crypto.subtle.sign('RSASSA-PKCS1-v1_5', pair.privateKey, enc.encode(input));
  const assertion = input + '.' + Buffer.from(signature).toString('base64url');
  let keyReads = 0;
  const verifyServiceAssertion = createServiceAssertionVerifier({ fetcher: async (url, options) => {
    assert.equal(url, issuer + '/cdn-cgi/access/certs'); assert.deepEqual(options.headers, { Accept: 'application/json' });
    keyReads++;
    return new Response(JSON.stringify({ keys: [jwk] }), { headers: { 'Content-Type': 'application/json' } });
  } });
  return { issuer, assertion, verifyServiceAssertion, keyReads: () => keyReads };
}

test('a genuine signed service assertion restores only draft access when identity is missing', async () => {
  const state = environment(), fixture = await signedFixture(); state.env.DRAFT_AUTOMATION_ISSUER = fixture.issuer;
  const missing = { access: { aud: AUD, getIdentity: async () => undefined } }, options = { verifyServiceAssertion: fixture.verifyServiceAssertion };
  const call = async (path, method = 'GET', raw) => {
    const req = request(path, method, raw); req.headers.set('Cf-Access-Jwt-Assertion', fixture.assertion);
    return handleDraftAutomationRequest(req, state.env, missing, options);
  };
  const index = await call(BASE + '/index'); assert.equal(index.status, 200); assert.deepEqual((await index.json()).index.issues, []);
  const raw = enc.encode('%PDF-1.7\nSigned private draft'), path = BASE + '/objects/issue-one/1/report-en.pdf';
  assert.equal((await call(path, 'PUT', raw)).status, 200);
  assert.deepEqual(new Uint8Array(await (await call(path)).arrayBuffer()), raw);
  const writes = state.calls.filter(([kind]) => kind === 'put').length;
  const forbidden = await call(BASE + '/index', 'POST'); assert.equal(forbidden.status, 405);
  assert.equal(state.calls.filter(([kind]) => kind === 'put').length, writes);
  assert.equal(state.objects.has('clients/client-one/release-index.json'), false); assert.equal(fixture.keyReads(), 1);
});

test('signed fallback works for an unavailable getter, never for an explicit wrong identity', async () => {
  const state = environment(), fixture = await signedFixture(); state.env.DRAFT_AUTOMATION_ISSUER = fixture.issuer;
  const options = { verifyServiceAssertion: fixture.verifyServiceAssertion }, req = request(BASE + '/index');
  req.headers.set('Cf-Access-Jwt-Assertion', fixture.assertion);
  for (const ctx of [{ access: { aud: AUD } }, { access: { aud: AUD, getIdentity: async () => { throw new Error('private lookup'); } } }]) {
    assert.equal((await handleDraftAutomationRequest(req, state.env, ctx, options)).status, 200);
  }
  const keysBefore = fixture.keyReads(), callsBefore = state.calls.length;
  for (const ctx of [{}, context(null, 'wrong-aud'), context({ email: 'owner@example.com' }), context([]),
    context({ service_token_id: ID, service_token_status: false }), context({ service_token_id: 'wrong.access', service_token_status: true })]) {
    assert.equal((await handleDraftAutomationRequest(req, state.env, ctx, options)).status, 403);
  }
  assert.equal(fixture.keyReads(), keysBefore); assert.equal(state.calls.length, callsBefore);
});

test('fallback requires exact true and exposes only fixed errors without any storage access', async () => {
  const req = request(BASE + '/index'); req.headers.set('Cf-Access-Jwt-Assertion', 'fixture.nonsecret.assertion');
  const missing = { access: { aud: AUD, getIdentity: async () => undefined } };
  for (const value of [false, undefined, 'true', {}]) {
    const state = environment(), result = await handleDraftAutomationRequest(req, state.env, missing,
      { verifyServiceAssertion: async () => value });
    assert.equal(result.status, 403); assert.equal(await result.text(), 'ADMIN_DRAFT_AUTOMATION_ASSERTION_INVALID');
    assert.deepEqual(state.calls, []);
  }
  for (const [error, code, status] of [[new ServiceAssertionError('KEYS_UNAVAILABLE'), 'ADMIN_DRAFT_AUTOMATION_ASSERTION_KEYS_UNAVAILABLE', 503],
    [new Error('private credential or provider body'), 'ADMIN_UNAVAILABLE', 503]]) {
    const state = environment(), result = await handleDraftAutomationRequest(req, state.env, missing,
      { verifyServiceAssertion: async () => { throw error; } });
    assert.equal(result.status, status); assert.equal(await result.text(), code); assert.deepEqual(state.calls, []);
  }
});
