// Bounded immutable release-index pages. R2's same-key conditional root is the
// sole authority; an orphan page, cursor or old descriptor grants no access.
export const LEGACY_RELEASE_KINDS = Object.freeze(['archive', 'issues', 'types', 'aliases', 'queue', 'holds', 'timeline', 'revisions']);
export const RELEASE_KINDS = Object.freeze([...LEGACY_RELEASE_KINDS, 'lineage']);
export const ROOT_LIMIT = 16 * 1024;
export const PAGE_LIMIT = 256 * 1024;
export const LEAF_LIMIT = 50;
const HASH = /^[a-f0-9]{64}$/, ID = /^[A-Za-z0-9_-]{1,80}$/, HEX = /^[a-f0-9]*$/;
const enc = new TextEncoder(), dec = new TextDecoder('utf-8', { fatal: true });
export class ReleasePageError extends Error {
  constructor(code, status = 503) { super(code); this.status = status; }
}
const fail = (code, status) => { throw new ReleasePageError(code, status); };
const integer = (v, min = 0) => Number.isSafeInteger(v) && v >= min;
const exact = (v, fields) => v && typeof v === 'object' && !Array.isArray(v) &&
  Object.keys(v).sort().join('|') === [...fields].sort().join('|');
export function releaseCanonical(value) {
  if (value === null || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'number') { if (!Number.isSafeInteger(value)) fail('RELEASE_NONINTEGER_JSON'); return JSON.stringify(value); }
  if (typeof value === 'string') { if (/[\uD800-\uDFFF]/u.test(value)) fail('RELEASE_INVALID_UNICODE'); return JSON.stringify(value); }
  if (Array.isArray(value)) return '[' + value.map(releaseCanonical).join(',') + ']';
  if (!value || typeof value !== 'object') fail('RELEASE_INVALID_JSON');
  const keys = Object.keys(value).sort((a, b) => {
    const x = Array.from(a, c => c.codePointAt(0)), y = Array.from(b, c => c.codePointAt(0));
    for (let i = 0; i < Math.min(x.length, y.length); i++) if (x[i] !== y[i]) return x[i] - y[i];
    return x.length - y.length;
  });
  return '{' + keys.map(k => releaseCanonical(k) + ':' + releaseCanonical(value[k])).join(',') + '}';
}
export const releaseBytes = v => enc.encode(releaseCanonical(v) + '\n');
export const releaseHash = async bytes => [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(v => v.toString(16).padStart(2, '0')).join('');
export const releaseDigest = value => releaseHash(enc.encode(releaseCanonical(value)));
export const releaseSequenceKey = n => { if (!integer(n, 1)) fail('RELEASE_SEQUENCE_INVALID'); return n.toString(16).padStart(16, '0'); };
export function releaseTime(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,3})?(?:Z|\+00:00)$/.test(value)) fail('RELEASE_DATE_INVALID');
  const at = Date.parse(value);
  if (!Number.isFinite(at) || new Date(at).toISOString().slice(0, 19) !== value.slice(0, 19)) fail('RELEASE_DATE_INVALID');
  return at;
}
export const releaseQueueKey = async entry => Math.max(releaseTime(entry.scheduled_at), releaseTime(entry.server_activated_at)).toString(16).padStart(16, '0') + await releaseDigest(entry.issue_id);
export const releaseHoldKey = async (type, issue) => await releaseDigest(type) + await releaseDigest(issue);
export function parseRelease(bytes) {
  let text, result;
  try { text = dec.decode(bytes); result = JSON.parse(text); } catch { fail('RELEASE_JSON_INVALID'); }
  const stack = [];
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (c === '{' || c === '[') { stack.push({ object: c === '{', key: c === '{', names: new Set() }); if (stack.length > 80) fail('RELEASE_JSON_DEPTH'); }
    else if (c === '}' || c === ']') stack.pop();
    else if (c === ',' && stack.at(-1)?.object) stack.at(-1).key = true;
    else if (c === '"') {
      const start = i; for (i++; i < text.length; i++) { if (text[i] === '\\') i++; else if (text[i] === '"') break; }
      const current = stack.at(-1);
      if (current?.object && current.key) { const name = JSON.parse(text.slice(start, i + 1)); if (current.names.has(name)) fail('RELEASE_JSON_DUPLICATE'); current.names.add(name); current.key = false; }
    }
  }
  releaseCanonical(result); return result;
}
export async function boundedReleaseBytes(object, limit) {
  if (!object || !integer(object.size, 1) || object.size > limit || typeof object.arrayBuffer !== 'function') fail('RELEASE_OBJECT_UNAVAILABLE');
  const bytes = new Uint8Array(await object.arrayBuffer());
  if (bytes.length !== object.size) fail('RELEASE_OBJECT_CHANGED');
  return bytes;
}
function keyLength(kind) { return ({ archive: 16, issues: 64, types: 64, aliases: 64, queue: 80, holds: 128, timeline: 32, revisions: 80, lineage: 129 })[kind]; }
function validKey(kind, key) { return typeof key === 'string' && HEX.test(key) && key.length === keyLength(kind); }
function descriptor(value, client, kind) {
  if (!exact(value, ['key', 'sha256', 'size', 'count']) || !HASH.test(value.sha256 ?? '') || !integer(value.size, 1) || value.size > PAGE_LIMIT || !integer(value.count, 1) ||
      value.key !== `clients/${client}/release-index-v2/pages/${kind}/${value.sha256}.json`) fail('RELEASE_DESCRIPTOR_INVALID');
  return value;
}
export function validateReleaseRoot(root, client, serverAt) {
  const fields = ['schema_version', 'contract', 'client_id', 'generation', 'previous_root_hash', 'updated_at', 'next_sequence', 'settled_publication_count', 'pages', 'migration'];
  if (!exact(root, fields) || root.schema_version !== 2 || root.contract !== 'release-index-v2' || root.client_id !== client || !ID.test(client) ||
      !integer(root.generation, 1) || !(root.previous_root_hash === null || HASH.test(root.previous_root_hash ?? '')) ||
      !integer(root.next_sequence, 1) || !integer(root.settled_publication_count) || !(exact(root.pages, LEGACY_RELEASE_KINDS)||exact(root.pages, RELEASE_KINDS)) || releaseBytes(root).length > ROOT_LIMIT) fail('RELEASE_ROOT_INVALID');
  if (releaseTime(root.updated_at) > releaseTime(serverAt)) fail('RELEASE_ROOT_FUTURE');
  if (root.migration !== null && (!exact(root.migration, ['source_etag', 'source_hash']) || !(root.migration.source_etag === null || typeof root.migration.source_etag === 'string' && root.migration.source_etag) || !HASH.test(root.migration.source_hash ?? ''))) fail('RELEASE_MIGRATION_INVALID');
  for (const kind of Object.keys(root.pages)) if (root.pages[kind] !== null) descriptor(root.pages[kind], client, kind);
  if ((root.pages.archive?.count ?? 0) !== root.next_sequence - 1 || (root.pages.revisions?.count ?? 0) !== (root.pages.archive?.count ?? 0) || root.settled_publication_count > (root.pages.issues?.count ?? 0)) fail('RELEASE_ROOT_COUNTS_INVALID');
  return root;
}
function validatePage(page, ref, root, kind, prefix) {
  const fields = ['contract', 'client_id', 'kind', 'generation', 'prefix', 'node', page?.node === 'branch' ? 'children' : 'entries'];
  if (!exact(page, fields) || page.contract !== 'release-index-page-v2' || page.client_id !== root.client_id || page.kind !== kind ||
      !integer(page.generation, 1) || page.generation > root.generation || page.prefix !== prefix || !HEX.test(prefix) || prefix.length > keyLength(kind)) fail('RELEASE_PAGE_BINDING_INVALID');
  if (page.node === 'branch') {
    if (!page.children || Array.isArray(page.children) || typeof page.children !== 'object' || !Object.keys(page.children).length || Object.keys(page.children).length > 16 || prefix.length >= keyLength(kind)) fail('RELEASE_BRANCH_INVALID');
    let count = 0;
    for (const [digit, child] of Object.entries(page.children)) { if (!/^[a-f0-9]$/.test(digit)) fail('RELEASE_BRANCH_INVALID'); descriptor(child, root.client_id, kind); count += child.count; }
    if (count !== ref.count) fail('RELEASE_PAGE_COUNT_INVALID');
  } else if (page.node === 'leaf') {
    if (!Array.isArray(page.entries) || !page.entries.length || page.entries.length > LEAF_LIMIT || page.entries.length !== ref.count) fail('RELEASE_LEAF_INVALID');
    let previous = null;
    for (const row of page.entries) {
      if (!exact(row, ['key', 'value']) || !validKey(kind, row.key) || !row.key.startsWith(prefix) || previous !== null && row.key <= previous || !row.value || typeof row.value !== 'object' || Array.isArray(row.value)) fail('RELEASE_LEAF_INVALID');
      previous = row.key;
    }
  } else fail('RELEASE_NODE_INVALID');
  return page;
}

// A request owns its cache, counters and pending objects. No global I/O state.
export class ReleaseTree {
  constructor(bucket, root, { readLimit = 160, writeLimit = 160 } = {}) {
    this.bucket = bucket; this.root = root; this.cache = new Map(); this.pending = new Map(); this.reads = 0; this.readLimit = readLimit; this.writeLimit = writeLimit;
  }
  async page(kind, ref, prefix = '') {
    descriptor(ref, this.root.client_id, kind);
    let page = this.cache.get(ref.key);
    if (!page) {
      if (++this.reads > this.readLimit) fail('RELEASE_READ_BUDGET', 409);
      const bytes = this.pending.get(ref.key)?.bytes ?? await boundedReleaseBytes(await this.bucket.get(ref.key), PAGE_LIMIT);
      if (bytes.length !== ref.size || await releaseHash(bytes) !== ref.sha256) fail('RELEASE_PAGE_HASH_INVALID');
      page = parseRelease(bytes); this.cache.set(ref.key, page);
    }
    return validatePage(page, ref, this.root, kind, prefix);
  }
  async get(kind, key) {
    if (!RELEASE_KINDS.includes(kind) || !validKey(kind, key)) fail('RELEASE_LOOKUP_INVALID', 400);
    let ref = this.root.pages[kind], prefix = '';
    while (ref) { const page = await this.page(kind, ref, prefix); if (page.node === 'leaf') { const found = page.entries.find(row => row.key === key); return found ? structuredClone(found.value) : null; }
      const digit = key[prefix.length]; prefix += digit; ref = page.children[digit] ?? null; }
    return null;
  }
  async scan(kind, { after = null, prefix = '', reverse = false, limit = 20, before = null } = {}) {
    if (!RELEASE_KINDS.includes(kind) || !integer(limit, 1) || limit > LEAF_LIMIT || !HEX.test(prefix) || prefix.length > keyLength(kind) ||
        after !== null && !validKey(kind, after) || before !== null && !validKey(kind, before)) fail('RELEASE_SCAN_INVALID', 400);
    const rows = []; let more = false;
    const walk = async (ref, path) => {
      if (!ref || more || !path.startsWith(prefix) && !prefix.startsWith(path)) return;
      const low = path.padEnd(keyLength(kind), '0'), high = path.padEnd(keyLength(kind), 'f');
      if (after !== null && (reverse ? low >= after : high <= after) || before !== null && low >= before) return;
      const page = await this.page(kind, ref, path);
      if (page.node === 'leaf') {
        const entries = reverse ? [...page.entries].reverse() : page.entries;
        for (const row of entries) {
          if (!row.key.startsWith(prefix) || after !== null && (reverse ? row.key >= after : row.key <= after) || before !== null && row.key >= before) continue;
          if (rows.length === limit) { more = true; break; } rows.push(structuredClone(row));
        }
      } else for (const digit of Object.keys(page.children).sort().sort((a, b) => reverse ? b.localeCompare(a) : a.localeCompare(b))) { await walk(page.children[digit], path + digit); if (more) break; }
    };
    await walk(this.root.pages[kind], '');
    return { rows, more, last_key: rows.at(-1)?.key ?? after };
  }
  async stage(kind, prefix, node) {
    const page = structuredClone({ contract: 'release-index-page-v2', client_id: this.root.client_id, kind, generation: this.root.generation, prefix, ...node });
    const bytes = releaseBytes(page);
    if (bytes.length > PAGE_LIMIT) fail('RELEASE_PAGE_LIMIT', 409);
    const sha256 = await releaseHash(bytes), key = `clients/${this.root.client_id}/release-index-v2/pages/${kind}/${sha256}.json`;
    const count = node.node === 'leaf' ? node.entries.length : Object.values(node.children).reduce((n, child) => n + child.count, 0);
    const ref = { key, sha256, size: bytes.length, count };
    validatePage(page, ref, this.root, kind, prefix);
    if (!this.pending.has(key) && this.pending.size >= this.writeLimit) fail('RELEASE_WRITE_BUDGET', 409);
    this.pending.set(key, { bytes, ref }); this.cache.set(key, page); return ref;
  }
  async build(kind, entries, prefix = '') {
    if (!entries.length) return null;
    if (entries.length <= LEAF_LIMIT) {
      const page = { contract: 'release-index-page-v2', client_id: this.root.client_id, kind, generation: this.root.generation, prefix, node: 'leaf', entries };
      if (releaseBytes(page).length <= PAGE_LIMIT) return this.stage(kind, prefix, { node: 'leaf', entries });
    }
    if (prefix.length >= keyLength(kind)) fail('RELEASE_ENTRY_LIMIT', 409);
    const groups = new Map();
    for (const row of entries) { if (!validKey(kind, row.key) || !row.key.startsWith(prefix)) fail('RELEASE_LOOKUP_INVALID', 400); const digit = row.key[prefix.length]; if (!groups.has(digit)) groups.set(digit, []); groups.get(digit).push(row); }
    const children = {};
    for (const digit of [...groups.keys()].sort()) children[digit] = await this.build(kind, groups.get(digit), prefix + digit);
    return this.stage(kind, prefix, { node: 'branch', children });
  }
  async set(kind, key, value) {
    if (!RELEASE_KINDS.includes(kind) || !validKey(kind, key) || value !== null && (!value || typeof value !== 'object' || Array.isArray(value))) fail('RELEASE_LOOKUP_INVALID', 400);
    if(value===null&&!Object.hasOwn(this.root.pages,kind))return;
    const update = async (ref, prefix) => {
      if (!ref) return value === null ? null : this.build(kind, [{ key, value }], prefix);
      const page = await this.page(kind, ref, prefix);
      if (page.node === 'leaf') {
        const entries = page.entries.filter(row => row.key !== key); if (value !== null) entries.push({ key, value }); entries.sort((a, b) => a.key.localeCompare(b.key));
        if (releaseCanonical(entries) === releaseCanonical(page.entries)) return ref;
        return this.build(kind, entries, prefix);
      }
      const digit = key[prefix.length], children = { ...page.children }, next = await update(children[digit] ?? null, prefix + digit);
      if (next) children[digit] = next; else delete children[digit];
      if (!Object.keys(children).length) return null;
      if (releaseCanonical(children) === releaseCanonical(page.children)) return ref;
      return this.stage(kind, prefix, { node: 'branch', children });
    };
    this.root.pages[kind] = await update(this.root.pages[kind], '');
    const reachable = this.reachablePending();
    for (const pendingKey of this.pending.keys()) if (!reachable.has(pendingKey)) this.pending.delete(pendingKey);
  }
  reachablePending() {
    const reachable = new Set();
    const visit = key => { if (reachable.has(key) || !this.pending.has(key)) return; const page = this.cache.get(key);
      if (page.node === 'branch') for (const ref of Object.values(page.children)) visit(ref.key); reachable.add(key); };
    for (const ref of Object.values(this.root.pages)) if (ref) visit(ref.key);
    return reachable;
  }
  async persist({ objects = null } = {}) {
    // Discard intermediate COW nodes from this request, not canonical history.
    // Only final reachable new pages need uploads and readback.
    const reachable = this.reachablePending();
    const selected = objects ?? [...reachable];
    for (const key of selected) {
      const staged = this.pending.get(key); if (!staged || !reachable.has(key)) fail('RELEASE_STAGE_OBJECT_INVALID');
      const { bytes, ref } = staged;
      await this.bucket.put(key, bytes, { onlyIf: new Headers({ 'If-None-Match': '*' }), httpMetadata: { contentType: 'application/json' } });
      const saved = await boundedReleaseBytes(await this.bucket.get(key), PAGE_LIMIT);
      if (saved.length !== ref.size || await releaseHash(saved) !== ref.sha256) fail('RELEASE_IMMUTABLE_PAGE_CONFLICT', 409);
    }
    return [...reachable];
  }
}

export function emptyReleaseRoot(client, at, migration = null) {
  return { schema_version: 2, contract: 'release-index-v2', client_id: client, generation: 1, previous_root_hash: null, updated_at: at,
    next_sequence: 1, settled_publication_count: 0, pages: Object.fromEntries(LEGACY_RELEASE_KINDS.map(kind => [kind, null])), migration };
}
export async function releaseCursor(root, kind, lastKey, options = {}) {
  const value = { contract: 'release-cursor-v2', client_id: root.client_id, root_hash: await releaseDigest(root), kind, last_key: lastKey, ...options };
  return btoa(String.fromCharCode(...enc.encode(releaseCanonical(value)))).replaceAll('+', '-').replaceAll('/', '_').replace(/=+$/, '');
}
export async function readReleaseCursor(token, root, kind) {
  if (typeof token !== 'string' || token.length > 2048 || !/^[A-Za-z0-9_-]+$/.test(token)) fail('RELEASE_CURSOR_INVALID', 400);
  let value; try { value = parseRelease(Uint8Array.from(atob(token.replaceAll('-', '+').replaceAll('_', '/')), c => c.charCodeAt(0))); } catch { fail('RELEASE_CURSOR_INVALID', 400); }
  if (!exact(value, ['contract', 'client_id', 'root_hash', 'kind', 'last_key', 'reverse', 'prefix']) || value.contract !== 'release-cursor-v2' || value.client_id !== root.client_id || value.kind !== kind ||
      value.root_hash !== await releaseDigest(root) || !validKey(kind, value.last_key) || typeof value.reverse !== 'boolean' || typeof value.prefix !== 'string' || !HEX.test(value.prefix)) fail('RELEASE_CURSOR_STALE', 409);
  return value;
}
