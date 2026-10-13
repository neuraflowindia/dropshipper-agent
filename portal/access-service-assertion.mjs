// A narrow fallback for an authenticated Access application whose trusted
// getIdentity() lookup is unavailable. The caller must still check ctx.access
// and the exact application audience; this module never grants owner access.
// https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/application-token/
// https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/
// RFC 7519 section 4.1.3 allows an audience string for a single recipient.
const PREFIX = 'ADMIN_DRAFT_AUTOMATION_ASSERTION_';
const CODES = Object.freeze({ CONFIG_INVALID: 503, REQUIRED: 403, INVALID: 403,
  SIGNATURE_INVALID: 403, KEY_UNKNOWN: 403, KEYS_UNAVAILABLE: 503 });
const CHECKS = new Set(['encoding', 'header', 'payload', 'type', 'issuer', 'audience',
  'audience_shape', 'audience_count', 'audience_format', 'audience_mismatch', 'subject', 'client', 'time']);
const ISSUER = /^https:\/\/[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com$/;
const AUDIENCE = /^[a-f0-9]{64}$/;
const CLIENT = /^[a-fA-F0-9]{32}\.access$/;
const KID = /^[A-Za-z0-9_-]{1,128}$/;
const HEADER_LIMIT = 16 * 1024, KEYS_LIMIT = 64 * 1024;
const CACHE_TTL = 5 * 60 * 1000, REFRESH_COOLDOWN = 30 * 1000;
const encoder = new TextEncoder(), decoder = new TextDecoder('utf-8', { fatal: true });
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const integer = value => Number.isSafeInteger(value) && value >= 0;

export class ServiceAssertionError extends Error {
  constructor(name = 'INVALID', check = null) {
    const safe = Object.hasOwn(CODES, name) ? name : 'INVALID';
    super(PREFIX + safe);
    this.name = 'ServiceAssertionError';
    this.code = PREFIX + safe;
    this.status = CODES[safe];
    // Fixed labels only. Never expose a claim, credential, token or exception.
    this.check = safe === 'INVALID' && CHECKS.has(check) ? check : null;
  }
}
const fail = (name, check = null) => { throw new ServiceAssertionError(name, check); };

function base64url(value, limit) {
  if (typeof value !== 'string' || !value || value.length > Math.ceil(limit * 4 / 3) ||
      !/^[A-Za-z0-9_-]+$/.test(value) || value.length % 4 === 1) fail('INVALID', 'encoding');
  let binary;
  try { binary = atob(value.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - value.length % 4) % 4)); }
  catch { fail('INVALID', 'encoding'); }
  if (!binary.length || binary.length > limit ||
      btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '') !== value) fail('INVALID', 'encoding');
  return Uint8Array.from(binary, ch => ch.charCodeAt(0));
}

function parse(bytes) {
  let text, value;
  try { text = decoder.decode(bytes); value = JSON.parse(text); } catch { fail('INVALID', 'encoding'); }
  // JSON grammar is checked above. Reject duplicate properties, including
  // escaped equivalent names, rather than letting JSON.parse choose a value.
  const stack = [];
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === '{' || ch === '[') {
      stack.push({ object: ch === '{', expectingKey: ch === '{', keys: new Set() });
      if (stack.length > 32) fail('INVALID', 'encoding');
    } else if (ch === '}' || ch === ']') stack.pop();
    else if (ch === ',') { if (stack.at(-1)?.object) stack.at(-1).expectingKey = true; }
    else if (ch === '"') {
      const start = i;
      for (i++; i < text.length; i++) { if (text[i] === '\\') i++; else if (text[i] === '"') break; }
      const current = stack.at(-1);
      if (current?.object && current.expectingKey) {
        const key = JSON.parse(text.slice(start, i + 1));
        if (current.keys.has(key)) fail('INVALID', 'encoding');
        current.keys.add(key); current.expectingKey = false;
      }
    }
  }
  return value;
}

function timeValid(payload, milliseconds) {
  const now = Math.floor(milliseconds / 1000);
  return integer(payload.iat) && integer(payload.exp) && payload.exp > payload.iat &&
    payload.exp > now && payload.iat <= now + 30 &&
    (!Object.hasOwn(payload, 'nbf') || integer(payload.nbf) && payload.nbf <= now + 30 && payload.nbf <= payload.exp);
}

function tokenParts(request, config, now) {
  let token;
  try { token = request.headers.get('Cf-Access-Jwt-Assertion'); } catch { fail('INVALID', 'encoding'); }
  if (token === null || token === '') fail('REQUIRED');
  if (typeof token !== 'string' || token.length > HEADER_LIMIT) fail('INVALID', 'encoding');
  const parts = token.split('.');
  if (parts.length !== 3) fail('INVALID', 'encoding');
  const header = parse(base64url(parts[0], 2048)), payload = parse(base64url(parts[1], 12 * 1024));
  const signature = base64url(parts[2], 1024);
  if (!object(header) || header.alg !== 'RS256' || typeof header.kid !== 'string' || !KID.test(header.kid) ||
      Object.hasOwn(header, 'typ') && header.typ !== 'JWT' ||
      ['crit', 'b64', 'jku', 'jwk', 'x5u'].some(field => Object.hasOwn(header, field))) fail('INVALID', 'header');
  // These unverified fields may only reject a request. They cannot authorize
  // one or choose a remote URL; signature verification is mandatory below.
  if (!object(payload)) fail('INVALID', 'payload');
  if (payload.type !== 'app') fail('INVALID', 'type');
  if (payload.iss !== config.issuer) fail('INVALID', 'issuer');
  const audiences = typeof payload.aud === 'string' ? [payload.aud] : payload.aud;
  if (!Array.isArray(audiences)) fail('INVALID', 'audience_shape');
  if (!audiences.length || audiences.length > 16) fail('INVALID', 'audience_count');
  if (audiences.some(value => typeof value !== 'string' || !AUDIENCE.test(value))) fail('INVALID', 'audience_format');
  if (!audiences.includes(config.audience)) fail('INVALID', 'audience_mismatch');
  if (payload.sub !== '') fail('INVALID', 'subject');
  if (payload.common_name !== config.clientId) fail('INVALID', 'client');
  if (!timeValid(payload, now)) fail('INVALID', 'time');
  return { kid: header.kid, payload, signature, signed: encoder.encode(parts[0] + '.' + parts[1]) };
}

async function importKeys(bytes) {
  const document = parse(bytes);
  if (!object(document) || !Array.isArray(document.keys) || !document.keys.length || document.keys.length > 16) fail('INVALID');
  const keys = new Map();
  for (const key of document.keys) {
    if (!object(key)) fail('INVALID');
    if (key.kty !== 'RSA' || Object.hasOwn(key, 'alg') && key.alg !== 'RS256' ||
        Object.hasOwn(key, 'use') && key.use !== 'sig') continue;
    if (typeof key.kid !== 'string' || !KID.test(key.kid) || keys.has(key.kid) ||
        ['d', 'p', 'q', 'dp', 'dq', 'qi', 'oth'].some(field => Object.hasOwn(key, field)) ||
        Object.hasOwn(key, 'key_ops') && (!Array.isArray(key.key_ops) || key.key_ops.length !== 1 || key.key_ops[0] !== 'verify')) fail('INVALID');
    const modulus = base64url(key.n, 1024), exponent = base64url(key.e, 8);
    const bits = (modulus.length - 1) * 8 + (32 - Math.clz32(modulus[0]));
    if (bits < 2048 || bits > 8192 || modulus[0] === 0 || exponent[0] === 0) fail('INVALID');
    const cryptoKey = await crypto.subtle.importKey('jwk', { kty: 'RSA', n: key.n, e: key.e, ext: true },
      { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['verify']);
    keys.set(key.kid, { key: cryptoKey, size: modulus.length });
  }
  if (!keys.size) fail('INVALID');
  return keys;
}

async function fetchKeys(fetcher, issuer, timeoutMs) {
  const controller = new AbortController();
  let reader, response, timedOut = false, timer;
  const cancel = () => {
    try {
      const pending = reader ? reader.cancel() : response?.body?.cancel();
      // Cleanup must not extend the deadline if a broken transport never ends.
      if (pending) Promise.resolve(pending).catch(() => {});
    } catch { /* Only public-key body cleanup; never an authorization fallback. */ }
  };
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => { timedOut = true; controller.abort(); cancel(); reject(new ServiceAssertionError('KEYS_UNAVAILABLE')); }, timeoutMs);
  });
  const operation = (async () => {
    const url = issuer + '/cdn-cgi/access/certs';
    response = await fetcher(url, { method: 'GET', redirect: 'manual', signal: controller.signal,
      headers: { Accept: 'application/json' } });
    if (timedOut) { cancel(); fail('KEYS_UNAVAILABLE'); }
    if (response.status !== 200 || response.redirected || response.url && response.url !== url ||
        !/^application\/json(?:\s*;|\s*$)/i.test(response.headers.get('content-type') ?? '') || !response.body) fail('KEYS_UNAVAILABLE');
    const length = response.headers.get('content-length');
    if (length !== null && (!/^\d+$/.test(length) || Number(length) > KEYS_LIMIT)) fail('KEYS_UNAVAILABLE');
    reader = response.body.getReader();
    const chunks = []; let size = 0;
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        if (!(value instanceof Uint8Array) || (size += value.byteLength) > KEYS_LIMIT) fail('KEYS_UNAVAILABLE');
        chunks.push(value);
      }
    } finally { reader.releaseLock(); reader = null; }
    if (!size) fail('KEYS_UNAVAILABLE');
    const bytes = new Uint8Array(size); let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    return importKeys(bytes);
  })();
  try { return await Promise.race([operation, deadline]); }
  catch { controller.abort(); cancel(); fail('KEYS_UNAVAILABLE'); }
  finally { clearTimeout(timer); }
}

export function createServiceAssertionVerifier({ fetcher = globalThis.fetch, clock = () => Date.now(), timeoutMs = 5000 } = {}) {
  if (typeof fetcher !== 'function' || typeof clock !== 'function' ||
      !Number.isSafeInteger(timeoutMs) || timeoutMs < 10 || timeoutMs > 10000) fail('CONFIG_INVALID');
  // One fixed issuer and small public-key set per Worker instance. No token or
  // user identity is cached. Unknown keys cannot trigger a fetch per request.
  let boundIssuer = null, keys = null, loadedAt = null, lastAttempt = null, inFlight = null;
  const now = () => {
    let value; try { value = clock(); } catch { fail('CONFIG_INVALID'); }
    if (!integer(value)) fail('CONFIG_INVALID');
    return value;
  };
  const cooling = value => lastAttempt !== null && value - lastAttempt < REFRESH_COOLDOWN;
  const fresh = value => keys !== null && value >= loadedAt && value - loadedAt < CACHE_TTL;
  const refresh = value => {
    if (inFlight) return inFlight;
    if (cooling(value)) fail('KEYS_UNAVAILABLE');
    lastAttempt = value;
    inFlight = (async () => {
      const acquired = await fetchKeys(fetcher, boundIssuer, timeoutMs);
      const acquiredAt = now();
      keys = acquired; loadedAt = acquiredAt;
      return acquired;
    })().finally(() => { inFlight = null; });
    return inFlight;
  };
  return async function verify(request, config) {
    if (!object(config) || typeof config.issuer !== 'string' || !ISSUER.test(config.issuer) ||
        typeof config.audience !== 'string' || !AUDIENCE.test(config.audience) ||
        typeof config.clientId !== 'string' || !CLIENT.test(config.clientId) ||
        boundIssuer !== null && boundIssuer !== config.issuer) fail('CONFIG_INVALID');
    boundIssuer = config.issuer;
    const token = tokenParts(request, config, now());
    let available = fresh(now()) ? keys : await refresh(now());
    if (!available.has(token.kid)) {
      // A warm cache refreshes once on rotation, never immediately after a
      // fetch and never repeatedly for arbitrary untrusted kid values.
      if (inFlight) available = await inFlight;
      else if (!cooling(now())) available = await refresh(now());
      if (!available.has(token.kid)) fail('KEY_UNKNOWN');
    }
    const selected = available.get(token.kid);
    if (token.signature.length !== selected.size) fail('SIGNATURE_INVALID');
    let valid = false;
    try { valid = await crypto.subtle.verify('RSASSA-PKCS1-v1_5', selected.key, token.signature, token.signed); }
    catch { fail('SIGNATURE_INVALID'); }
    if (!valid) fail('SIGNATURE_INVALID');
    if (!timeValid(token.payload, now())) fail('INVALID', 'time');
    return true;
  };
}
