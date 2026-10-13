import test from 'node:test';
import assert from 'node:assert/strict';
import { createServiceAssertionVerifier, ServiceAssertionError } from './access-service-assertion.mjs';

// All signatures and certificate responses are generated locally. No account,
// saved service credential, network endpoint or canonical product data is used.
const CONFIG = Object.freeze({ issuer: 'https://broad-snow-3543.cloudflareaccess.com',
  audience: 'a'.repeat(64), clientId: 'b'.repeat(32) + '.access' });
const BASE = Date.UTC(2026, 9, 8, 9), SECOND = BASE / 1000;
const ALGORITHM = { name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048,
  publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' };
const first = await crypto.subtle.generateKey(ALGORITHM, true, ['sign', 'verify']);
const second = await crypto.subtle.generateKey(ALGORITHM, true, ['sign', 'verify']);
const publicKey = async (pair, kid) => ({ ...await crypto.subtle.exportKey('jwk', pair.publicKey), kid, alg: 'RS256', use: 'sig' });
const FIRST = await publicKey(first, 'key-one'), SECOND_KEY = await publicKey(second, 'key-two');
const b64 = value => Buffer.from(typeof value === 'string' ? value : JSON.stringify(value)).toString('base64url');
const claims = patch => ({ type: 'app', iss: CONFIG.issuer, aud: [CONFIG.audience],
  iat: SECOND - 10, exp: SECOND + 3600, sub: '', common_name: CONFIG.clientId, ...patch });
async function signed({ payload = claims(), header = { alg: 'RS256', kid: FIRST.kid, typ: 'JWT' }, pair = first } = {}) {
  const input = b64(header) + '.' + b64(payload);
  const signature = await crypto.subtle.sign('RSASSA-PKCS1-v1_5', pair.privateKey, new TextEncoder().encode(input));
  return input + '.' + Buffer.from(signature).toString('base64url');
}
const request = value => new Request('https://reports.example/_automation/drafts/index',
  { headers: value == null ? {} : { 'Cf-Access-Jwt-Assertion': value } });
const response = (keys = [FIRST], options = {}) => new Response(JSON.stringify({ keys }),
  { headers: { 'Content-Type': 'application/json' }, ...options });
function fixture({ fetcher, clock, timeoutMs } = {}) {
  const calls = [], state = { time: BASE };
  const verify = createServiceAssertionVerifier({ clock: clock ?? (() => state.time), timeoutMs: timeoutMs ?? 100,
    fetcher: async (...args) => { calls.push(args); return fetcher ? fetcher(...args) : response(); } });
  return { verify, calls, state };
}
async function rejected(operation, suffix, status = suffix === 'KEYS_UNAVAILABLE' || suffix === 'CONFIG_INVALID' ? 503 : 403) {
  await assert.rejects(operation, error => {
    assert.ok(error instanceof ServiceAssertionError);
    assert.equal(error.code, 'ADMIN_DRAFT_AUTOMATION_ASSERTION_' + suffix);
    assert.equal(error.message, error.code);
    assert.equal(error.status, status);
    assert.equal(error.cause, undefined);
    return true;
  });
}

test('accepts a real signed exact service assertion with a fixed public-key request only', async () => {
  const { verify, calls } = fixture();
  assert.equal(await verify(request(await signed()), CONFIG), true);
  assert.equal(calls.length, 1);
  const [url, options] = calls[0];
  assert.equal(url, CONFIG.issuer + '/cdn-cgi/access/certs');
  assert.equal(options.method, 'GET'); assert.equal(options.redirect, 'manual');
  assert.deepEqual(options.headers, { Accept: 'application/json' });
  assert.ok(options.signal instanceof AbortSignal);
  assert.equal(options.body, undefined);
});

test('supports optional nbf and another valid audience without relaxing the exact audience', async () => {
  const { verify } = fixture();
  assert.equal(await verify(request(await signed({ payload: claims({ nbf: SECOND - 5, aud: ['c'.repeat(64), CONFIG.audience] }) })), CONFIG), true);
});

test('supports a real signed exact single audience string, as required by RFC 7519', async () => {
  const { verify, calls } = fixture();
  assert.equal(await verify(request(await signed({ payload: claims({ aud: CONFIG.audience }) })), CONFIG), true);
  assert.equal(calls.length, 1);
});

test('invalid-assertion diagnostics identify only a fixed check, never the rejected value', async () => {
  for (const [check, patch] of Object.entries({ type: { type: 'private-type' },
    issuer: { iss: 'https://private-issuer.example' }, audience_shape: { aud: { private: CONFIG.audience } },
    audience_count: { aud: [] }, audience_format: { aud: [17] }, audience_mismatch: { aud: ['c'.repeat(64)] },
    subject: { sub: 'private-subject' }, client: { common_name: 'private-client' }, time: { exp: SECOND } })) {
    const { verify, calls } = fixture();
    await assert.rejects(verify(request(await signed({ payload: claims(patch) })), CONFIG), error => {
      assert.equal(error.code, 'ADMIN_DRAFT_AUTOMATION_ASSERTION_INVALID');
      assert.equal(error.check, check);
      assert.equal(error.message, error.code);
      assert.equal(error.status, 403);
      assert.equal(error.cause, undefined);
      return true;
    });
    assert.equal(calls.length, 0);
  }
  assert.equal(new ServiceAssertionError('INVALID', 'private-token').check, null);
  assert.equal(new ServiceAssertionError('KEYS_UNAVAILABLE', 'client').check, null);
});

test('requires assertion rather than an owner cookie or forged Client ID header', async () => {
  const { verify, calls } = fixture();
  await rejected(verify(new Request('https://reports.example', { headers: {
    Cookie: 'CF_Authorization=private-owner', 'CF-Access-Client-Id': CONFIG.clientId,
  } }), CONFIG), 'REQUIRED');
  assert.equal(calls.length, 0);
});

test('rejects malformed, oversized and noncanonical assertion encodings before fetching', async t => {
  const valid = await signed(), parts = valid.split('.');
  for (const [name, token] of Object.entries({ missing: null, empty: '', noSignature: parts.slice(0, 2).join('.'),
    extraPart: valid + '.extra', padding: parts[0] + '=.' + parts[1] + '.' + parts[2],
    oversized: 'x'.repeat(16 * 1024 + 1), duplicateHeaders: valid + ', ' + valid,
    invalidCharacters: '*' + valid, truncatedEncoding: 'a.' + parts[1] + '.' + parts[2] })) {
    await t.test(name, async () => {
      const { verify, calls } = fixture();
      await rejected(verify(request(token), CONFIG), name === 'missing' || name === 'empty' ? 'REQUIRED' : 'INVALID');
      assert.equal(calls.length, 0);
    });
  }
});

test('rejects noncanonical unused base64url trailing bits', async () => {
  const token = await signed(), parts = token.split('.');
  // A 2048-bit signature is 256 bytes: its final character has four unused
  // bits. Changing only those bits must not create a second accepted encoding.
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
  const original = alphabet.indexOf(parts[2].at(-1));
  assert.equal(original % 16, 0);
  parts[2] = parts[2].slice(0, -1) + alphabet[original + 1];
  const { verify, calls } = fixture();
  await rejected(verify(request(parts.join('.')), CONFIG), 'INVALID');
  assert.equal(calls.length, 0);
});

test('rejects unsafe or nonfixed configured issuers and invalid config', async t => {
  const token = await signed();
  for (const [name, patch] of Object.entries({ http: { issuer: 'http://broad-snow-3543.cloudflareaccess.com' },
    userinfo: { issuer: 'https://user@broad-snow-3543.cloudflareaccess.com' },
    path: { issuer: CONFIG.issuer + '/' }, query: { issuer: CONFIG.issuer + '?url=private' },
    port: { issuer: 'https://broad-snow-3543.cloudflareaccess.com:443' },
    fakeSuffix: { issuer: CONFIG.issuer + '.example.com' },
    localhost: { issuer: 'https://localhost' }, audience: { audience: 'wrong' }, client: { clientId: 'wrong' },
    arrayAudience: { audience: [CONFIG.audience] }, arrayClient: { clientId: [CONFIG.clientId] } })) {
    await t.test(name, async () => {
      const { verify, calls } = fixture();
      await rejected(verify(request(token), { ...CONFIG, ...patch }), 'CONFIG_INVALID');
      assert.equal(calls.length, 0);
    });
  }
  const { verify, calls } = fixture();
  await verify(request(token), CONFIG);
  await rejected(verify(request(token), { ...CONFIG, issuer: 'https://another-team.cloudflareaccess.com' }), 'CONFIG_INVALID');
  assert.equal(calls.length, 1);
});

test('rejects algorithm substitutions, key injection and unsupported JOSE extensions', async t => {
  for (const [name, patch] of Object.entries({ none: { alg: 'none' }, hmac: { alg: 'HS256' },
    typ: { typ: 'owner' }, crit: { crit: [] }, unencoded: { b64: false },
    remoteKey: { jku: 'https://private.example/keys' }, injectedKey: { jwk: FIRST },
    certificateURL: { x5u: 'https://private.example/cert' }, missingKid: { kid: null }, arrayKid: { kid: ['key-one'] } })) {
    await t.test(name, async () => {
      const { verify, calls } = fixture();
      await rejected(verify(request(await signed({ header: { alg: 'RS256', kid: FIRST.kid, ...patch } })), CONFIG), 'INVALID');
      assert.equal(calls.length, 0);
    });
  }
});

test('rejects signed wrong service, owner, issuer, audience and token-type claims', async t => {
  for (const [name, patch] of Object.entries({ issuer: { iss: 'https://private.example' },
    audience: { aud: ['c'.repeat(64)] }, wrongStringAudience: { aud: 'c'.repeat(64) },
    emptyAudience: { aud: [] }, numericAudience: { aud: [1] },
    client: { common_name: 'c'.repeat(32) + '.access' }, missingClient: { common_name: null },
    owner: { sub: 'owner-user-uuid', email: 'private-owner@example.com' },
    missingSubject: { sub: null }, globalSession: { type: 'org' } })) {
    await t.test(name, async () => {
      const { verify, calls } = fixture();
      await rejected(verify(request(await signed({ payload: claims(patch) })), CONFIG), 'INVALID');
      assert.equal(calls.length, 0);
    });
  }
});

test('rejects expired, future, unordered and noninteger times', async t => {
  for (const [name, patch] of Object.entries({ expired: { exp: SECOND },
    futureIssue: { iat: SECOND + 31 }, futureUse: { nbf: SECOND + 31 },
    notBeforeExpiry: { nbf: SECOND + 4000 }, unordered: { exp: SECOND - 20 },
    stringTime: { exp: String(SECOND + 3600) }, missingIssue: { iat: null },
    fractional: { iat: SECOND - 0.5 }, negative: { iat: -1 }, boolTime: { nbf: false } })) {
    await t.test(name, async () => {
      const { verify, calls } = fixture();
      await rejected(verify(request(await signed({ payload: claims(patch) })), CONFIG), 'INVALID');
      assert.equal(calls.length, 0);
    });
  }
});

test('rechecks expiry after public-key retrieval rather than using stale prefetch time', async () => {
  let time = BASE;
  const { verify } = fixture({ clock: () => time, fetcher: async () => { time += 2000; return response(); } });
  await rejected(verify(request(await signed({ payload: claims({ exp: SECOND + 1 }) })), CONFIG), 'INVALID');
});

test('rejects duplicate or malformed JSON fields, non-UTF8 and excessive nesting', async t => {
  const badHeader = '{"alg":"RS256","kid":"key-one","\\u006bid":"key-one"}';
  const badPayload = JSON.stringify(claims()).replace('"type":"app"', '"type":"app","type":"app"');
  const deepPayload = { ...claims(), nested: Array.from({ length: 34 }).reduce(value => [value], 0) };
  for (const [name, value] of Object.entries({ headerDuplicates: await signed({ header: badHeader }),
    payloadDuplicates: await signed({ payload: badPayload }),
    depth: await signed({ payload: deepPayload }),
    invalidUTF8: Buffer.from([0xff]).toString('base64url') + '.' + b64(claims()) + '.AQAB' })) {
    await t.test(name, async () => {
      const { verify, calls } = fixture();
      await rejected(verify(request(value), CONFIG), 'INVALID');
      assert.equal(calls.length, 0);
    });
  }
});

test('rejects forged and tampered signatures without refreshing a known key', async () => {
  const { verify, calls } = fixture();
  await verify(request(await signed()), CONFIG);
  await rejected(verify(request(await signed({ pair: second })), CONFIG), 'SIGNATURE_INVALID');
  const token = await signed(), [head, payload, signature] = token.split('.');
  const changed = { ...JSON.parse(Buffer.from(payload, 'base64url').toString()), private_label: 'forged' };
  await rejected(verify(request(head + '.' + b64(changed) + '.' + signature), CONFIG), 'SIGNATURE_INVALID');
  await rejected(verify(request(head + '.' + payload + '.AQAB'), CONFIG), 'SIGNATURE_INVALID');
  assert.equal(calls.length, 1);
});

test('fetch errors never leak credentials, assertion, provider body or exception', async () => {
  const token = await signed();
  const { verify } = fixture({ fetcher: async () => { throw new Error('private-client-secret:' + token); } });
  await rejected(verify(request(token), CONFIG), 'KEYS_UNAVAILABLE');
});

test('rejects redirects, bad status, wrong content type and wrong final key URL', async t => {
  for (const [name, fetcher] of Object.entries({ redirect: async () => new Response('private', { status: 302, headers: { Location: 'https://private.example' } }),
    failure: async () => new Response('private-provider-error', { status: 500 }),
    contentType: async () => response([FIRST], { headers: { 'Content-Type': 'text/html' } }),
    wrongURL: async () => { const value = response(); Object.defineProperty(value, 'url', { value: 'https://private.example/certs' }); return value; },
    followedRedirect: async () => { const value = response(); Object.defineProperty(value, 'redirected', { value: true }); return value; } })) {
    await t.test(name, async () => { const { verify } = fixture({ fetcher }); await rejected(verify(request(await signed()), CONFIG), 'KEYS_UNAVAILABLE'); });
  }
});

test('bounds key response content length and actual streamed body', async t => {
  for (const [name, fetcher] of Object.entries({ declaredSize: async () => response([FIRST],
    { headers: { 'Content-Type': 'application/json', 'Content-Length': String(64 * 1024 + 1) } }),
    invalidLength: async () => response([FIRST], { headers: { 'Content-Type': 'application/json', 'Content-Length': 'private' } }),
    actualSize: async () => new Response('x'.repeat(64 * 1024 + 1), { headers: { 'Content-Type': 'application/json' } }),
    emptyBody: async () => new Response('', { headers: { 'Content-Type': 'application/json' } }) })) {
    await t.test(name, async () => { const { verify } = fixture({ fetcher }); await rejected(verify(request(await signed()), CONFIG), 'KEYS_UNAVAILABLE'); });
  }
});

test('rejects malformed, private, duplicate and weak public signing keys', async t => {
  for (const [name, keys] of Object.entries({ empty: [], duplicates: [FIRST, FIRST],
    private: [{ ...FIRST, d: 'private-material' }], weak: [{ ...FIRST, n: Buffer.alloc(64, 255).toString('base64url') }],
    noncanonical: [{ ...FIRST, e: 'AQAB=' }], wrongOps: [{ ...FIRST, key_ops: ['sign'] }],
    wrongAlgorithm: [{ ...FIRST, alg: 'HS256' }], wrongUse: [{ ...FIRST, use: 'enc' }],
    tooMany: Array.from({ length: 17 }, (_, i) => ({ ...FIRST, kid: 'key-' + i })) })) {
    await t.test(name, async () => {
      const { verify } = fixture({ fetcher: async () => response(keys) });
      await rejected(verify(request(await signed()), CONFIG), 'KEYS_UNAVAILABLE');
    });
  }
  const { verify } = fixture({ fetcher: async () => new Response('{"keys":[],"keys":[]}', { headers: { 'Content-Type': 'application/json' } }) });
  await rejected(verify(request(await signed()), CONFIG), 'KEYS_UNAVAILABLE');
});

test('fetch timeout aborts even a transport ignoring abort and limits repeated retries', async () => {
  let signal;
  const { verify, calls } = fixture({ timeoutMs: 10, fetcher: async (_, options) => { signal = options.signal; return new Promise(() => {}); } });
  const token = await signed(), start = Date.now();
  await rejected(verify(request(token), CONFIG), 'KEYS_UNAVAILABLE');
  assert.ok(Date.now() - start < 1000); assert.equal(signal.aborted, true);
  await rejected(verify(request(token), CONFIG), 'KEYS_UNAVAILABLE');
  assert.equal(calls.length, 1);
});

test('whole deadline also bounds a stalled streamed body and cancels it', async () => {
  let signal, canceled = false;
  const { verify } = fixture({ timeoutMs: 10, fetcher: async (_, options) => {
    signal = options.signal;
    return new Response(new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode('{"keys":[')); },
      cancel() { canceled = true; } }), { headers: { 'Content-Type': 'application/json' } });
  } });
  await rejected(verify(request(await signed()), CONFIG), 'KEYS_UNAVAILABLE');
  assert.equal(signal.aborted, true); assert.equal(canceled, true);
});

test('warm cache reuses public keys with no extra transport requests', async () => {
  const { verify, calls } = fixture();
  const token = await signed();
  for (let i = 0; i < 5; i++) assert.equal(await verify(request(token), CONFIG), true);
  assert.equal(calls.length, 1);
});

test('concurrent cold verification shares one in-flight public-key fetch', async () => {
  let release;
  const waiting = new Promise(resolve => { release = resolve; });
  const { verify, calls } = fixture({ fetcher: async () => { await waiting; return response(); } });
  const token = await signed();
  const pending = Array.from({ length: 8 }, () => verify(request(token), CONFIG));
  release(); assert.deepEqual(await Promise.all(pending), Array(8).fill(true));
  assert.equal(calls.length, 1);
});

test('arbitrary unknown key IDs cannot cause a refresh request per assertion', async () => {
  const { verify, calls } = fixture();
  await verify(request(await signed()), CONFIG);
  for (let i = 0; i < 8; i++) await rejected(verify(request(await signed({ header: { alg: 'RS256', kid: 'unknown-' + i } })), CONFIG), 'KEY_UNKNOWN');
  assert.equal(calls.length, 1);
});

test('rotation refreshes once after cooldown and verifies new signed key', async () => {
  let changed = false;
  const { verify, calls, state } = fixture({ fetcher: async () => response(changed ? [FIRST, SECOND_KEY] : [FIRST]) });
  await verify(request(await signed()), CONFIG);
  const rotated = await signed({ header: { alg: 'RS256', kid: SECOND_KEY.kid }, pair: second });
  await rejected(verify(request(rotated), CONFIG), 'KEY_UNKNOWN');
  changed = true; state.time += 30 * 1000;
  assert.equal(await verify(request(rotated), CONFIG), true);
  assert.equal(await verify(request(rotated), CONFIG), true);
  assert.equal(calls.length, 2);
});

test('concurrent rotated assertions share one finite refresh', async () => {
  let changed = false, release;
  const waiting = new Promise(resolve => { release = resolve; });
  const { verify, calls, state } = fixture({ fetcher: async () => {
    if (changed) { await waiting; return response([FIRST, SECOND_KEY]); }
    return response();
  } });
  await verify(request(await signed()), CONFIG);
  changed = true; state.time += 30 * 1000;
  const token = await signed({ header: { alg: 'RS256', kid: SECOND_KEY.kid }, pair: second });
  const pending = Array.from({ length: 8 }, () => verify(request(token), CONFIG));
  assert.equal(calls.length, 2);
  release(); assert.deepEqual(await Promise.all(pending), Array(8).fill(true));
  assert.equal(calls.length, 2);
});

test('unknown key refresh failure does not discard a still-fresh known public key', async () => {
  let healthy = true;
  const { verify, calls, state } = fixture({ fetcher: async () => {
    if (!healthy) throw new Error('private-refresh-details'); return response();
  } });
  const original = await signed(); await verify(request(original), CONFIG);
  state.time += 30 * 1000; healthy = false;
  const unknown = await signed({ header: { alg: 'RS256', kid: 'unknown' } });
  await rejected(verify(request(unknown), CONFIG), 'KEYS_UNAVAILABLE');
  assert.equal(await verify(request(original), CONFIG), true);
  await rejected(verify(request(unknown), CONFIG), 'KEY_UNKNOWN');
  assert.equal(calls.length, 2);
});

test('a late successful fetch after timeout cannot populate or revive the cache', async () => {
  let release;
  const waiting = new Promise(resolve => { release = resolve; });
  const { verify, calls } = fixture({ timeoutMs: 10, fetcher: async () => { await waiting; return response(); } });
  const token = await signed();
  await rejected(verify(request(token), CONFIG), 'KEYS_UNAVAILABLE');
  release(); await new Promise(resolve => setTimeout(resolve, 0));
  await rejected(verify(request(token), CONFIG), 'KEYS_UNAVAILABLE');
  assert.equal(calls.length, 1);
});

test('cache expiry refreshes rather than trusting indefinitely stale certificates', async () => {
  const { verify, calls, state } = fixture();
  const token = await signed();
  await verify(request(token), CONFIG); state.time += 5 * 60 * 1000;
  await verify(request(token), CONFIG); assert.equal(calls.length, 2);
});

test('expired-cache transport failure stays closed and retries only after cooldown', async () => {
  let healthy = true;
  const { verify, calls, state } = fixture({ fetcher: async () => { if (!healthy) throw new Error('private'); return response(); } });
  const token = await signed();
  await verify(request(token), CONFIG); state.time += 5 * 60 * 1000; healthy = false;
  await rejected(verify(request(token), CONFIG), 'KEYS_UNAVAILABLE');
  await rejected(verify(request(token), CONFIG), 'KEYS_UNAVAILABLE');
  assert.equal(calls.length, 2); healthy = true; state.time += 30 * 1000;
  assert.equal(await verify(request(token), CONFIG), true); assert.equal(calls.length, 3);
});

test('bounded constructor options and clock failures are safe configuration errors', async () => {
  for (const patch of [{ timeoutMs: 0 }, { timeoutMs: 10001 }, { timeoutMs: NaN }, { fetcher: null }, { clock: null }]) {
    assert.throws(() => createServiceAssertionVerifier(patch), error => error instanceof ServiceAssertionError && error.status === 503);
  }
  for (const clock of [() => NaN, () => -1, () => { throw new Error('private-clock-details'); }]) {
    const { verify } = fixture({ clock }); await rejected(verify(request(await signed()), CONFIG), 'CONFIG_INVALID');
  }
  const error = new ServiceAssertionError('private-message');
  assert.equal(error.message, 'ADMIN_DRAFT_AUTOMATION_ASSERTION_INVALID');
});
