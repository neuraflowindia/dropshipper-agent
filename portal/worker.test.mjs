import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { handleRequest, validateIndex } from './worker.mjs';

const NOW = Date.parse('2026-10-13T02:30:00Z'); // 08:00 India time.
const CLIENT = 'client-one';
const INDEX = `clients/${CLIENT}/release-index.json`;
const PDF = new TextEncoder().encode('%PDF-1.7\nfixture');
const XLSX = new TextEncoder().encode('PK fixture workbook');
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const clone = value => structuredClone(value);

function fixture(overrides = {}) {
  const issue = {
    issue_id: 'issue-2026-10-13', revision: 1, title: 'Product picks', summary: 'Ten products worth a closer look.',
    scheduled_at: '2026-10-13T02:30:00Z', server_activated_at: '2026-10-12T09:00:00Z',
    remote_state: 'active', approved: true, complete: true, product_count: 10, new_count: 8,
    package_hash: 'a'.repeat(64), publication_ordinal: 1, languages: ['en', 'hi-Latn'], ...overrides,
  };
  const files = ['report-en.pdf', 'report-hi-Latn.pdf', 'shortlist.xlsx'].map(name => ({
    name, key: `clients/${CLIENT}/issues/${issue.issue_id}/r${issue.revision}/${name}`,
    size: name.endsWith('.pdf') ? PDF.byteLength : XLSX.byteLength,
    sha256: sha(name.endsWith('.pdf') ? PDF : XLSX),
    media_type: name.endsWith('.pdf') ? 'application/pdf' : 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  }));
  issue.files ??= files;
  return { schema_version: 1, client_id: CLIENT, issues: [issue] };
}

function context(email = 'client@example.com', aud = 'approved-worker-aud') {
  return { access: { aud, getIdentity: async () => ({ email }) } };
}

function environment(index = fixture(), objectOverrides = {}) {
  const reads = [];
  const objects = new Map();
  if (index !== null) objects.set(INDEX, new TextEncoder().encode(JSON.stringify(index)));
  for (const issue of index?.issues ?? []) for (const file of issue.files) objects.set(file.key, file.name.endsWith('.pdf') ? PDF : XLSX);
  for (const [key, value] of Object.entries(objectOverrides)) objects.set(key, value);
  const env = { CLIENT_ID: CLIENT, ACCESS_AUD: 'approved-worker-aud', ALLOWED_EMAILS: '["client@example.com"]', OPERATOR_EMAILS: '["operator@example.com"]', REPORTS: {
    async get(key) {
      reads.push(key);
      const bytes = objects.get(key);
      if (bytes === undefined || bytes === null) return null;
      if (!(bytes instanceof Uint8Array)) return bytes;
      return { size: bytes.byteLength, async arrayBuffer() { return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength); } };
    },
  } };
  return { env, reads, objects };
}

function request(path = '/reports', method = 'GET', headers = {}) {
  return new Request('https://reports.example.com' + path, { method, headers });
}

function path(index, name = 'report-en.pdf') {
  const issue = index.issues[0];
  return `/reports/download/${issue.issue_id}/${issue.revision}/${name}`;
}

test('signed-out pages and direct files deny before storage reads, even with forged headers', async () => {
  for (const route of ['/', '/archive', '/reports', '/reports/', '/reports/archive', '/reports/archive/',
    path(fixture()), path(fixture()).replace('/reports/download/', '/download/')]) {
    const { env, reads } = environment();
    const result = await handleRequest(request(route, 'GET', { 'CF-Access-Authenticated-User-Email': 'client@example.com', 'Cf-Access-Jwt-Assertion': 'invented-token' }), env, {}, NOW);
    assert.equal(result.status, 403);
    assert.deepEqual(reads, []);
    assert.match(result.headers.get('Cache-Control'), /no-store/);
  }
});

test('wrong audience, unapproved Gmail, absent identity and identity failures deny', async () => {
  for (const ctx of [context('client@example.com', 'other-aud'), context('unknown@gmail.com'),
    { access: { aud: 'approved-worker-aud', getIdentity: async () => undefined } },
    { access: { aud: 'approved-worker-aud', getIdentity: async () => { throw new Error('private error'); } } }]) {
    const { env, reads } = environment();
    const result = await handleRequest(request(), env, ctx, NOW);
    assert.equal(result.status, 403);
    assert.deepEqual(reads, []);
  }
  const { env } = environment();
  assert.equal((await handleRequest(request(), env, { access: { aud: 'approved-worker-aud', getIdentity: async () => ({}) } }, NOW)).status, 403);
});

test('empty client list denies every report and download before storage access', async () => {
  for (const route of ['/reports', '/reports/archive', path(fixture())]) {
    const { env, reads } = environment(); env.ALLOWED_EMAILS = '[]';
    const result = await handleRequest(request(route), env, context(), NOW);
    assert.equal(result.status, 403);
    assert.deepEqual(reads, []);
  }
});

test('invalid or overlapping operator roles deny client routes before storage', async () => {
  for (const operators of [undefined, '[]', 'not-json', '["CLIENT@example.com"]', '["invalid"]']) {
    for (const route of ['/reports', path(fixture())]) {
      const { env, reads } = environment(); env.OPERATOR_EMAILS = operators;
      const response = await handleRequest(request(route), env, context(), NOW);
      assert.equal(response.status, 503);
      assert.deepEqual(reads, []);
    }
  }
});

test('only approved email is accepted without echoing the identity', async () => {
  const { env } = environment();
  const result = await handleRequest(request(), env, context('CLIENT@EXAMPLE.COM'), NOW);
  assert.equal(result.status, 200);
  const html = await result.text();
  assert.match(html, /NeuraFlow/);
  assert.match(html, /Download PDF/);
  assert.match(html, /Hinglish/);
  assert.match(html, /Download Excel/);
  assert.doesNotMatch(html, /client@example.com/i);
});

test('canonical report pages link to the agreed pages and protected download URLs', async () => {
  for (const route of ['/reports', '/reports/archive']) {
    const { env } = environment();
    const result = await handleRequest(request(route), env, context(), NOW);
    assert.equal(result.status, 200);
    const html = await result.text();
    assert.match(html, /href="\/reports"/);
    assert.match(html, /href="\/reports\/archive"/);
    assert.match(html, /href="\/reports\/download\/issue-2026-10-13\/1\/report-en\.pdf"/);
    assert.doesNotMatch(html, /href="\/(?:archive|download\/|"\s)/);
    assert.match(html, route.endsWith('/archive') ? /href="\/reports\/archive" aria-current="page"/ : /href="\/reports" aria-current="page"/);
    const head = await handleRequest(request(route, 'HEAD'), env, context(), NOW);
    assert.equal(head.status, 200);
    assert.equal(await head.text(), '');
  }
});

test('legacy pages and trailing slashes redirect only after sign-in and without storage reads', async () => {
  for (const [route, target] of [['/', '/reports'], ['/archive', '/reports/archive'],
    ['/reports/', '/reports'], ['/reports/archive/', '/reports/archive']]) {
    for (const method of ['GET', 'HEAD']) {
      const { env, reads } = environment();
      const result = await handleRequest(request(route + '?view=latest', method), env, context(), NOW);
      assert.equal(result.status, 308);
      assert.equal(result.headers.get('Location'), target + '?view=latest');
      assert.equal(await result.text(), '');
      assert.match(result.headers.get('Cache-Control'), /no-store/);
      assert.deepEqual(reads, []);
      const anonymous = await handleRequest(request(route, method), env, {}, NOW);
      assert.equal(anonymous.status, 403);
      assert.equal(anonymous.headers.get('Location'), null);
      const forbidden = await handleRequest(request(route, 'POST'), env, context(), NOW);
      assert.equal(forbidden.status, 405);
      assert.equal(forbidden.headers.get('Location'), null);
      assert.deepEqual(reads, []);
    }
  }
});

test('canonical and legacy download links enforce the same release and integrity checks', async () => {
  for (const prefix of ['/reports/download/', '/download/']) {
    const index = fixture(), { env, reads } = environment(index);
    const route = path(index).replace('/reports/download/', prefix);
    assert.equal((await handleRequest(request(route), env, context(), NOW - 1)).status, 404);
    assert.deepEqual(reads, [INDEX]);
    const file = await handleRequest(request(route), env, context(), NOW);
    assert.equal(file.status, 200);
    assert.deepEqual(new Uint8Array(await file.arrayBuffer()), PDF);
    const head = await handleRequest(request(route, 'HEAD'), env, context(), NOW);
    assert.equal(head.status, 200);
    assert.equal(await head.text(), '');
    const corrupt = PDF.slice(); corrupt[0] = 0;
    const broken = environment(index, { [index.issues[0].files[0].key]: corrupt });
    assert.equal((await handleRequest(request(route), broken.env, context(), NOW)).status, 503);
  }
});

test('unknown report paths and unsupported methods do not touch storage', async () => {
  for (const route of ['/reports/unknown', '/reports/archive/extra', '/reports/download/private.pdf', '/reports/admin/status']) {
    const { env, reads } = environment();
    assert.equal((await handleRequest(request(route), env, context(), NOW)).status, 404);
    assert.deepEqual(reads, []);
  }
  for (const route of ['/reports', '/reports/archive', path(fixture())]) {
    const { env, reads } = environment();
    assert.equal((await handleRequest(request(route, 'POST'), env, context(), NOW)).status, 405);
    assert.deepEqual(reads, []);
  }
});

test('future issue and future activation are not shown or read by guessed download URLs', async () => {
  for (const overrides of [{ scheduled_at: '2026-10-15T02:30:00Z' }, { server_activated_at: '2026-10-13T02:30:01Z' }]) {
    const index = fixture(overrides), { env, reads } = environment(index);
    assert.equal((await handleRequest(request(path(index)), env, context(), NOW)).status, 404);
    assert.deepEqual(reads, [INDEX]);
    const html = await (await handleRequest(request(), env, context(), NOW)).text();
    assert.doesNotMatch(html, /Ten products/);
    assert.match(html, /when it is ready/);
  }
});

test('release starts at exact server UTC instant, not browser time or query arguments', async () => {
  const index = fixture(), { env, reads } = environment(index);
  assert.equal((await handleRequest(request(path(index) + '?now=2099-01-01'), env, context(), NOW - 1)).status, 404);
  assert.deepEqual(reads, [INDEX]);
  assert.equal((await handleRequest(request(path(index)), env, context(), NOW)).status, 200);
});

test('approval, completeness, ten-product floor and eighty-percent novelty remain actual release requirements', async () => {
  for (const overrides of [{ approved: false }, { complete: false }, { product_count: 9, new_count: 8 }, { new_count: 7 }, { remote_state: 'withdrawn' }]) {
    const index = fixture(overrides), { env, reads } = environment(index);
    assert.equal((await handleRequest(request(path(index)), env, context(), NOW)).status, 404);
    assert.deepEqual(reads, [INDEX]);
  }
});

test('pending language selection is retained but never shown as ready', async () => {
  const index = fixture({ publication_ordinal: null, complete: false, languages: [], files: [] });
  assert.equal(validateIndex(index, CLIENT), index);
  const { env } = environment(index);
  assert.match(await (await handleRequest(request(), env, context(), NOW)).text(), /when it is ready/);
});

test('first two logical issues require both PDFs; later issues require one language and the workbook', async () => {
  for (const ordinal of [1, 2]) {
    const index = fixture({ publication_ordinal: ordinal, languages: ['en'] });
    index.issues[0].files = index.issues[0].files.filter(file => file.name !== 'report-hi-Latn.pdf');
    assert.throws(() => validateIndex(index, CLIENT));
  }
  const later = fixture({ publication_ordinal: 3, languages: ['en'] });
  later.issues[0].files = later.issues[0].files.filter(file => file.name !== 'report-hi-Latn.pdf');
  assert.equal(validateIndex(later, CLIENT), later);
  assert.throws(() => validateIndex(fixture({ publication_ordinal: 3 }), CLIENT));
});

test('invalid manifests deny rather than exposing unchecked filenames or another client', async () => {
  const mutations = [
    index => { index.client_id = 'other-client'; },
    index => { index.issues[0].files[0].key = 'clients/other-client/private.pdf'; },
    index => { index.issues[0].files[0].sha256 = 'not-a-hash'; },
    index => { index.issues[0].files[0].name = 'private.html'; },
    index => { index.issues[0].scheduled_at = '2026-02-31T02:30:00Z'; },
    index => { index.issues[0].scheduled_at = '2026-10-13T08:00:00+05:30'; },
    index => { index.issues[0].files.pop(); },
    index => { index.issues[0].files[0].size = 40 * 1024 * 1024; },
  ];
  for (const mutate of mutations) {
    const index = fixture(); mutate(index);
    const { env, reads } = environment(index);
    const result = await handleRequest(request(), env, context(), NOW);
    assert.equal(result.status, 503);
    assert.equal(await result.text(), 'Report temporarily unavailable.');
    assert.deepEqual(reads, [INDEX]);
  }
});

test('old withdrawn revision is unavailable, while distinct active revision works', async () => {
  const current = fixture({ revision: 2 });
  current.issues.push(fixture({ remote_state: 'withdrawn' }).issues[0]);
  const { env } = environment(current);
  assert.equal((await handleRequest(request(path(fixture())), env, context(), NOW)).status, 404);
  assert.equal((await handleRequest(request(path(current)), env, context(), NOW)).status, 200);
  const duplicated = clone(current); duplicated.issues[1].remote_state = 'active';
  assert.throws(() => validateIndex(duplicated, CLIENT));
  assert.throws(() => validateIndex({ ...current, issues: [current.issues[0], clone(current.issues[0])] }, CLIENT));
});

test('hash mismatch, missing object or unexpected object size returns only a generic error', async () => {
  const index = fixture(), key = index.issues[0].files[0].key;
  const corrupt = PDF.slice(); corrupt[0] = 0;
  for (const object of [corrupt, null, { size: 100, arrayBuffer: async () => { throw new Error('secret'); } }]) {
    const { env } = environment(index, { [key]: object });
    const result = await handleRequest(request(path(index)), env, context(), NOW);
    assert.equal(result.status, 503);
    assert.equal(await result.text(), 'Report temporarily unavailable.');
  }
});

test('GET serves only manifest-listed bytes with fixed types and attachment names', async () => {
  const index = fixture(), { env } = environment(index);
  for (const name of ['report-en.pdf', 'report-hi-Latn.pdf', 'shortlist.xlsx']) {
    const result = await handleRequest(request(path(index, name)), env, context(), NOW);
    assert.equal(result.status, 200);
    assert.equal(result.headers.get('Content-Disposition'), `attachment; filename="${name}"`);
    assert.equal(result.headers.get('X-Content-Type-Options'), 'nosniff');
    assert.deepEqual(new Uint8Array(await result.arrayBuffer()), name.endsWith('.pdf') ? PDF : XLSX);
  }
  assert.equal((await handleRequest(request(path(index, 'facts.json')), env, context(), NOW)).status, 404);
  assert.equal((await handleRequest(request('/download/../../private.pdf'), env, context(), NOW)).status, 404);
});

test('HEAD verifies the same identity/release/integrity but returns no body', async () => {
  const index = fixture(), { env } = environment(index);
  const download = await handleRequest(request(path(index), 'HEAD'), env, context(), NOW);
  assert.equal(download.status, 200);
  assert.equal(download.headers.get('Content-Length'), String(PDF.length));
  assert.equal(await download.text(), '');
  assert.equal(await (await handleRequest(request('/', 'HEAD'), env, context(), NOW)).text(), '');
  assert.equal((await handleRequest(request(path(index), 'HEAD'), env, {}, NOW)).status, 403);
});

test('no upload route exists and unsupported methods cannot read or change storage', async () => {
  const { env, reads } = environment();
  const result = await handleRequest(request('/', 'POST'), env, context(), NOW);
  assert.equal(result.status, 405);
  assert.equal(result.headers.get('Allow'), 'GET, HEAD');
  assert.deepEqual(reads, []);
});

test('empty index is useful readiness page; storage failures are not an empty success', async () => {
  const { env } = environment(null);
  assert.match(await (await handleRequest(request(), env, context(), NOW)).text(), /when it is ready/);
  env.REPORTS.get = async () => { throw new Error('private bucket failure'); };
  assert.equal((await handleRequest(request(), env, context(), NOW)).status, 503);
});

test('source text is escaped and pages have no external asset or script requests', async () => {
  const index = fixture({ title: '<script>alert("x")</script>', summary: '<img src="https://unsafe.example/x">' });
  const { env } = environment(index);
  const result = await handleRequest(request('/reports/archive'), env, context(), NOW);
  const html = await result.text();
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /<script|<link/i);
  assert.match(html, /<img src="data:image\/png;base64,/);
  assert.doesNotMatch(html, /<img[^>]+src="https?:/i);
  assert.match(result.headers.get('Content-Security-Policy'), /default-src 'none'/);
});

test('index and artifact object sizes are checked before reading the body', async () => {
  const { env } = environment();
  let consumed = false;
  env.REPORTS.get = async () => ({ size: 1024 * 1024, arrayBuffer: async () => { consumed = true; return new ArrayBuffer(0); } });
  assert.equal((await handleRequest(request(), env, context(), NOW)).status, 503);
  assert.equal(consumed, false);
});
