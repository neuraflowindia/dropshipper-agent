// Entirely manufactured fixture bytes and in-memory fake R2. No hosted requests.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { handleAdminRequest, applyReleaseMutation, canonical, projectOrdinal, validateFitPacket } from './release-admin.mjs';
import { handleRequest, validateIndex } from './worker.mjs';

const BEFORE = '2026-10-12T10:00:00.000Z', AFTER = '2026-10-13T03:00:00.000Z', SCHEDULE = '2026-10-13T02:30:00+00:00';
const CLIENT = 'client-one', INDEX = `clients/${CLIENT}/release-index.json`;
const enc = new TextEncoder(), sha = bytes => createHash('sha256').update(bytes).digest('hex');
const encoded = value => enc.encode(canonical(value) + '\n');
const context = (email = 'operator@example.com', aud = 'worker-aud') => ({ access: { aud, getIdentity: async () => ({ email }) } });
const fixed = value => ({ clock: () => new Date(value) });
function request(path, method = 'GET', bytes, headers = {}) {
  return new Request('https://reports.example.com' + path, { method, ...(bytes ? { body: bytes } : {}),
    headers: { 'X-NeuraFlow-Release-Action': 'v1', ...headers } });
}
function fakeR2() {
  const objects = new Map(), calls = []; let version = 0;
  return { objects, calls, conflict: false, failIndex: false, corrupt: false,
    async get(key) {
      calls.push(['get', key]);
      const saved = objects.get(key); if (!saved) return null;
      const bytes = saved.bytes;
      return { size: bytes.byteLength, etag: saved.etag, arrayBuffer: async () => bytes.slice().buffer };
    },
    async put(key, input, options = {}) {
      calls.push(['put', key, options]);
      if (key === INDEX && this.failIndex) throw new Error('private-provider-message-must-not-escape');
      if (key === INDEX && this.conflict) return null;
      const old = objects.get(key), condition = options.onlyIf;
      if (condition instanceof Headers && condition.get('If-None-Match') === '*' && old) return null;
      if (condition?.etagMatches !== undefined && old?.etag !== condition.etagMatches) return null;
      const bytes = new Uint8Array(input).slice();
      const etag = `actual-r2-etag-${++version}`;
      objects.set(key, { bytes, etag });
      return { etag, uploaded: new Date(BEFORE) };
    } };
}
function environment() {
  const bucket = fakeR2();
  return { CLIENT_ID: CLIENT, ACCESS_AUD: 'worker-aud', ALLOWED_EMAILS: '["client@example.com"]', OPERATOR_EMAILS: '["operator@example.com"]',
    PUBLICATION_TIME: '08:00', ONGOING_LANGUAGE: '', REPORTS: bucket };
}
function packet(issue = 'issue-one', revision = 1, ordinal = 1, date = '2026-10-13') {
  const languages = ordinal <= 2 ? ['en', 'hi-Latn'] : ['en'];
  const products = Array.from({ length: 10 }, (_, i) => ({ concept_id: `${issue}-product-${i}`, rank: i + 1, title: `Manufactured fixture ${i}`,
    category: ['Home and Decor', 'Baby Products', 'Home and Kitchen', 'Travel and Outdoor'][i % 4], status: 'New',
    kpi_states: ['Meets', 'Meets', 'Not assessed', ...Array(5).fill('Meets'), ...Array(5).fill('Unclear')], kpi_matches: 7 }));
  // "captured" exercises the captured-data wire format using fictional fixture
  // values; it does not establish genuine evidence or produce client outputs.
  const facts = { schema_version: 1, evidence_type: 'captured', issue_id: issue, revision, release_date: date, selection_hash: 'b'.repeat(64), products };
  const path = `clients/${CLIENT}/issues/${issue}/r${revision}/`, rawFacts = encoded(facts);
  const objects = new Map([[path + 'final_report_data.json', rawFacts]]);
  const artifacts = { facts: { path: '/local/final_report_data.json', sha256: sha(rawFacts), size: rawFacts.length } };
  const files = ['shortlist.xlsx', ...languages.map(l => `report-${l}.pdf`)].map(name => {
    const raw = enc.encode(name.endsWith('.pdf') ? '%PDF-1.7\nManufactured fixture, not a real report.' : 'PK manufactured fixture workbook');
    objects.set(path + name, raw);
    const record = { path: '/local/' + name, sha256: sha(raw), size: raw.length };
    artifacts[name === 'shortlist.xlsx' ? 'workbook' : 'pdf-' + name.slice(7, -4)] = record;
    return { name, key: path + name, sha256: record.sha256, size: raw.length, media_type: name.endsWith('.pdf') ? 'application/pdf' : 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' };
  });
  const manifest = { schema_version: 1, client_id: CLIENT, issue_id: issue, revision, release_date: date, evidence_type: 'captured', selection_hash: facts.selection_hash,
    facts_hash: sha(enc.encode(canonical(facts))), complete: true, approved: false, release_ready: false, publication_ordinal: ordinal,
    concept_ids: products.map(p => p.concept_id), counts: { products: 10, new: 10, updates: 0 }, product_count: 10, new_count: 10, languages, files, artifacts };
  manifest.package_hash = sha(enc.encode(canonical(manifest)));
  objects.set(path + 'delivery-manifest.json', encoded(manifest));
  return { manifest, facts, objects };
}
function activation(p, { previous = null, schedule = SCHEDULE, late = false, selected = null } = {}) {
  const m = p.manifest;
  return { kind: 'activate', client_id: CLIENT, manifest: m, scheduled_at: schedule, expected_previous: previous, selected_language: selected, allow_synthetic: false,
    approval: { approved: true, owner_id: 'owner-one', client_id: CLIENT, issue_id: m.issue_id, revision: m.revision, package_hash: m.package_hash,
      selection_hash: m.selection_hash, publication_ordinal: m.publication_ordinal, scheduled_at: schedule, ...(late ? { allow_late: true } : {}) } };
}
const FIT_IDS = [1, 2, 5, 6, 7, 8, 9, 11, 12, 13];
function currentPacket(matches = 5) {
  const p = packet();
  Object.assign(p.facts, { schema_version: 2, fit_policy_id: 'fit-10-v1', criterion_ids: FIT_IDS, kpi_total: 10, narratives: {} });
  Object.assign(p.manifest, { fit_policy_id: 'fit-10-v1', criterion_ids: FIT_IDS, kpi_total: 10, workbook_contract: 'fit-checklist-v1' });
  for (const product of p.facts.products) {
    const description = { label: product.title, function: 'Fictional household use', brand: 'unclear' };
    Object.assign(product, { fit_policy_id: 'fit-10-v1', criterion_ids: FIT_IDS, kpi_total: 10, kpi_matches: matches,
      fit_tier: matches >= 6 ? 'good' : 'decent', kpi_states: FIT_IDS.map((_, i) => i < matches ? 'Meets' : 'Unclear'),
      supplier_effort_satisfied: true, source_refs: [{ id: 'source-one' }],
      supplier_assessment_ids: ['legacy-assessment'], supplier_search_check_ids: [], suppliers: [], supplier_leads_found: 0,
      description, supplier_search_status: 'complete',
      brand_binding: { verdict: 'unclear', source: 'original', review_id: null, description_hash: sha(enc.encode(canonical(description))) },
      source_revisions: { supplier_assessments: { 'legacy-assessment': 'c'.repeat(64) } },
      supplier_effort: { check_ids: [], actual_attempt_refs: [], last_attempt_at: null, internal_outcome: 'not_attempted',
        effort_satisfied: false, usable_lead_count: 0, legacy_interpretation_ids: ['legacy-assessment'], legacy_search_check_ids: [] },
      criteria: FIT_IDS.map((id, i) => ({ id, state: i < matches ? 'Meets' : 'Unclear', basis: i < matches ? 'inferred' : 'not_stated',
        references: i < matches ? ['source-one'] : [], reason: 'Fictional criterion judgment.' })) });
  }
  for (const language of p.manifest.languages) p.facts.narratives[language] = { title: 'Fictional report',
    products: p.facts.products.map(product => ({ concept_id: product.concept_id,
      checklist_phrases: FIT_IDS.map(id => ({ id, text: language === 'en' ? 'Fixture judgment' : 'Test ki rai' })) })) };
  refreshFacts(p);
  return p;
}
function refreshFacts(p) {
  const path = `clients/${CLIENT}/issues/${p.manifest.issue_id}/r${p.manifest.revision}/`, raw = encoded(p.facts);
  p.objects.set(path + 'final_report_data.json', raw);
  Object.assign(p.manifest.artifacts.facts, { sha256: sha(raw), size: raw.length });
  p.manifest.facts_hash = sha(enc.encode(canonical(p.facts)));
  delete p.manifest.package_hash; p.manifest.package_hash = sha(enc.encode(canonical(p.manifest)));
  p.objects.set(path + 'delivery-manifest.json', encoded(p.manifest));
}

function descriptionPacket() {
  const p = platformPacket();
  for (const product of p.facts.products) {
    product.fit_description_contract = 'product-description-inference-v1';
    for (const row of product.criteria) {
      row.description_inference = null;
      if (row.state === 'Meets') Object.assign(row, { references: [], reason: 'Estimated: ordinary handling takes fewer steps.',
        description_inference: { kind: product.fit_description_contract,
          description_hash: product.brand_binding.description_hash, fields: ['function'] } });
    }
  }
  refreshFacts(p); return p;
}

test('description-grounded estimates remain separate from market sources and keep historical packets readable', () => {
  const p = descriptionPacket();
  assert.equal(validateFitPacket(p.facts, p.manifest), p.facts);
  assert.equal(p.facts.products[0].kpi_matches, 6);
  assert.deepEqual(p.facts.products[0].opportunity_basis.source_ids, ['amazon', 'flipkart']);
  assert.equal(p.facts.products[0].criteria[0].references.length, 0);
  const old = currentPacket(); assert.equal(validateFitPacket(old.facts, old.manifest), old.facts);
});

test('criterion reason limits count the same Unicode characters as Python', () => {
  const p = descriptionPacket(), row = p.facts.products[0].criteria[0];
  row.reason = 'Estimated: ' + '🙂'.repeat(1989);
  assert.equal(Array.from(row.reason).length, 2000);
  assert.equal(validateFitPacket(p.facts, p.manifest), p.facts);
  row.reason += '🙂';
  assert.throws(() => validateFitPacket(p.facts, p.manifest), /ADMIN_FACT_REQUIREMENTS_NOT_MET/);
});

test('description estimates cannot copy another description, add exact figures or pretend to be observations', () => {
  const changes = [
    product => { delete product.fit_description_contract; },
    product => { product.fit_description_contract = 'invented'; },
    product => { delete product.criteria[9].description_inference; },
    product => { product.criteria[0].description_inference.description_hash = 'f'.repeat(64); },
    product => { product.criteria[0].description_inference.description_hash = ['f'.repeat(64)]; },
    product => { product.criteria[0].description_inference.kind = 'invented'; },
    product => { product.criteria[0].description_inference.fields = []; },
    product => { product.criteria[0].description_inference.fields = ['function', 'function']; },
    product => { product.criteria[0].description_inference.fields = ['brand']; },
    product => { product.criteria[0].description_inference.fields = ['features']; },
    product => { product.criteria[0].description_inference.extra = 'invented'; },
    product => { product.criteria[0].basis = 'observed'; },
    product => { product.criteria[0].references = ['source-one']; },
    product => { product.criteria[0].reason = 'Saves time.'; },
    product => { product.criteria[0].reason = 'Estimated: saves 10 minutes.'; },
    product => { product.criteria[0].reason = 'Estimated: weighs 0.25 kg.'; },
    product => { product.criteria[0].reason = 'Estimated: costs ₹250.'; },
    product => { product.criteria[0].reason = 'Estimated: has 100 sales.'; },
    product => { product.criteria[9].description_inference = product.criteria[0].description_inference; },
  ];
  for (const change of changes) {
    const p = descriptionPacket(); change(p.facts.products[0]);
    assert.throws(() => validateFitPacket(p.facts, p.manifest), /ADMIN_(FIT_DESCRIPTION_|FACT_REQUIREMENTS_NOT_MET)/);
  }
});

test('description estimate reasons handle ordinary negative judgments without treating missing evidence as failure', () => {
  const p = descriptionPacket(), row = p.facts.products[0].criteria[6];
  Object.assign(row, { state: 'Does not meet', basis: 'inferred', references: [], reason: 'Estimated: no recurring expense is avoided.',
    description_inference: structuredClone(p.facts.products[0].criteria[0].description_inference) });
  p.facts.products[0].kpi_states[6] = 'Does not meet';
  assert.equal(validateFitPacket(p.facts, p.manifest), p.facts);
  row.state = 'Unclear'; p.facts.products[0].kpi_states[6] = 'Unclear';
  assert.throws(() => validateFitPacket(p.facts, p.manifest), /ADMIN_FIT_DESCRIPTION_INFERENCE_INVALID/);
});

test('new estimate attribution cannot silently change a historical thirteen-criterion packet', () => {
  const p = packet(); p.facts.products[0].fit_description_contract = 'product-description-inference-v1';
  assert.throws(() => validateFitPacket(p.facts, p.manifest), /ADMIN_FACT_REQUIREMENTS_NOT_MET/);
});

test('marked priorities preserve the four-tier order and historical packets remain readable', () => {
  for (const [matches, sources, positive, priority] of [[6, 2, 0, 1], [5, 2, 0, 2], [6, 1, 1, 3], [5, 1, 1, 4]]) {
    const p = currentPacket(matches);
    p.facts.opportunity_policy_id = p.manifest.opportunity_policy_id = 'opportunity-priority-v1';
    for (const product of p.facts.products) Object.assign(product, { opportunity_policy_id: 'opportunity-priority-v1',
      opportunity_priority: priority, opportunity_basis: { available_families: sources, positive_families: positive } });
    assert.equal(validateFitPacket(p.facts, p.manifest), p.facts);
    const changed = structuredClone(p.facts); changed.products[0].opportunity_priority = priority === 4 ? 1 : 4;
    assert.throws(() => validateFitPacket(changed, p.manifest), /ADMIN_OPPORTUNITY_PRIORITY_INVALID/);
  }
  const legacy = currentPacket(); assert.equal(validateFitPacket(legacy.facts, legacy.manifest), legacy.facts);
});

test('priority markers cannot turn absent, partial or one-nonpositive support into confirmation', () => {
  for (const mutate of [p => { delete p.facts.products[0].opportunity_basis; },
    p => { p.facts.products[0].opportunity_basis.available_families = true; },
    p => { p.facts.products[0].opportunity_basis = { available_families: 1, positive_families: 0 }; },
    p => { delete p.manifest.opportunity_policy_id; }]) {
    const p = currentPacket(); p.facts.opportunity_policy_id = p.manifest.opportunity_policy_id = 'opportunity-priority-v1';
    for (const product of p.facts.products) Object.assign(product, { opportunity_policy_id: 'opportunity-priority-v1',
      opportunity_priority: 2, opportunity_basis: { available_families: 2, positive_families: 0 } });
    mutate(p); assert.throws(() => validateFitPacket(p.facts, p.manifest), /ADMIN_OPPORTUNITY_/);
  }
});

function platformPacket(matches = 6, sources = ['amazon', 'flipkart'], positive = []) {
  const p = currentPacket(matches), ids = [...sources].sort(), positiveIds = [...positive].sort();
  p.facts.opportunity_policy_id = p.manifest.opportunity_policy_id = 'opportunity-priority-v2';
  for (const product of p.facts.products) {
    const evidence = Object.fromEntries(ids.map(source => [source, [`source-${source}`]]));
    Object.assign(product, { opportunity_policy_id: 'opportunity-priority-v2',
      opportunity_priority: ids.length >= 2 ? (matches >= 6 ? 1 : 2) : (matches >= 6 ? 3 : 4),
      opportunity_basis: { available_sources: ids.length, positive_sources: positiveIds.length,
        source_ids: ids, positive_source_ids: positiveIds, evidence_by_source: evidence },
      source_refs: [...product.source_refs, ...ids.map(source => ({ id: `source-${source}` }))] });
  }
  refreshFacts(p); return p;
}

const SOURCE_ROUTE_FIXTURE = [
  ['meta-ads', 'Facebook Ads Library', 'meta'],
  ['amazon-bestsellers', 'Amazon India Bestsellers', 'marketplace'],
  ['amazon-search', 'Amazon India product search', 'marketplace'],
  ['flipkart-search', 'Flipkart product search', 'marketplace'],
  ['google-trends', 'Google Trends India', 'trends'],
  ['blinkit', 'Blinkit', 'quickcommerce'], ['zepto', 'Zepto', 'quickcommerce'], ['instamart', 'Instamart', 'quickcommerce'],
  ['google-suppliers', 'Google supplier search', 'supplier'], ['google-maps', 'Google Maps supplier search', 'supplier'],
  ['indiamart', 'IndiaMART', 'supplier'], ['tradeindia', 'TradeIndia', 'supplier'], ['company-pages', 'Supplier company pages', 'supplier'],
  ['deodap', 'DeoDap', 'dropship-platforms'], ['baapstore', 'BaapStore', 'dropship-platforms'],
  ['dropship-india', 'Dropship India', 'dropship-platforms'], ['dropdash', 'Dropdash', 'dropship-platforms'],
];
function sourcePacket() {
  const p = platformPacket();
  for (const product of p.facts.products) {
    product.source_revisions.observations = Object.fromEntries(product.source_refs.map(ref => [ref.id, 'a'.repeat(64)]));
    product.source_revisions.source_checks = { 'receipt-one': 'c'.repeat(64) };
    product.source_revisions.query_tasks = { 'task-one': 'd'.repeat(64) };
    Object.assign(product, { image_path: null, source_route_contract: 'saved-source-routes-v1',
      source_route_checks: SOURCE_ROUTE_FIXTURE.map(([route, source, source_role]) => ({ route, source, source_role,
        description_hash: product.brand_binding.description_hash, outcome: 'not_attempted', state: 'Not tried', actual_attempt: false,
        detail: 'No product check is saved for this route.', measurements: [], evidence_ids: [], task_ids: [], receipt_ids: [],
        observed_at: null, url: null, last_attempt: null })) });
    const row = product.source_route_checks.find(row => row.route === 'google-trends');
    Object.assign(row, { outcome: 'completed_relevant', state: 'Relevant result found', actual_attempt: true,
      detail: 'Synthetic fractional chart sample; not actual market data.', evidence_ids: ['source-one'], task_ids: ['task-one'],
      receipt_ids: ['receipt-one'], observed_at: BEFORE, url: 'https://trends.google.com/trends/explore?geo=IN&q=fixture',
      measurements: [{ name: 'chart_intervals', value: 53, evidence_ids: ['source-one'] },
        { name: 'previous_mean_bounds', value: ['1.25', '2.75'], evidence_ids: ['source-one'] },
        { name: 'recent_mean_bounds', value: ['8.125', '12.5'], evidence_ids: ['source-one'] }],
      last_attempt: { outcome: 'usable', actual_attempt: true, at: BEFORE, receipt_id: 'receipt-one' } });
  }
  refreshFacts(p); return p;
}

test('marked source facts keep every exact route and fractional chart figures without relaxing canonical hashes', () => {
  const p = sourcePacket(); assert.equal(validateFitPacket(p.facts, p.manifest), p.facts);
  assert.equal(p.facts.products[0].source_route_checks.length, SOURCE_ROUTE_FIXTURE.length);
  const raw = canonical(p.facts);
  assert.ok(raw.includes('"value":["8.125","12.5"]'));
  assert.throws(() => canonical({ value: 8.125 }), /ADMIN_NONINTEGER_JSON/);
  assert.equal(validateFitPacket(platformPacket().facts, platformPacket().manifest).schema_version, 2);
});

test('new source facts omit the retired Google Search route; earlier full-row packets stay valid', () => {
  const p = sourcePacket();
  for (const product of p.facts.products) product.source_route_checks = product.source_route_checks.filter(row => row.route !== 'google-suppliers');
  refreshFacts(p);
  assert.equal(validateFitPacket(p.facts, p.manifest), p.facts);
  assert.equal(p.facts.products[0].source_route_checks.length, SOURCE_ROUTE_FIXTURE.length - 1);
  const shifted = sourcePacket();
  for (const product of shifted.facts.products) product.source_route_checks = product.source_route_checks.filter(row => row.route !== 'google-maps');
  refreshFacts(shifted);
  assert.throws(() => validateFitPacket(shifted.facts, shifted.manifest), /ADMIN_SOURCE_ROUTE_FACTS_INVALID/);
  // The deeper Meta read has no client row of its own (it folds into the Meta row).
  const deeper = sourcePacket();
  for (const product of deeper.facts.products) product.source_route_checks.push({ ...product.source_route_checks[0],
    route: 'meta-ads-depth', source: 'Facebook Ads Library (deeper read)' });
  refreshFacts(deeper);
  assert.throws(() => validateFitPacket(deeper.facts, deeper.manifest), /ADMIN_SOURCE_ROUTE_FACTS_INVALID/);
});

test('supplier detail version preserves absent legacy markers and accepts current source-bound metadata', () => {
  for (const make of [packet, currentPacket, platformPacket, sourcePacket]) {
    const p = make();
    assert.equal(validateFitPacket(p.facts, p.manifest), p.facts);
  }
  const p = sourcePacket();
  p.facts.products[0].supplier_terms_display_version = 'supplier-terms-v2';
  refreshFacts(p);
  assert.equal(validateFitPacket(p.facts, p.manifest), p.facts);
});

test('supplier detail version rejects unknown values and incompatible packet scope', () => {
  for (const value of [null, true, 2, [], {}, '', 'supplier-terms-v1', 'invented']) {
    const p = sourcePacket();
    p.facts.products[0].supplier_terms_display_version = value;
    refreshFacts(p);
    assert.throws(() => validateFitPacket(p.facts, p.manifest), /ADMIN_SUPPLIER_TERMS_DISPLAY_VERSION_INVALID/);
  }
  for (const make of [packet, currentPacket, platformPacket]) {
    const p = make();
    p.facts.products[0].supplier_terms_display_version = 'supplier-terms-v2';
    refreshFacts(p);
    assert.throws(() => validateFitPacket(p.facts, p.manifest), /ADMIN_SUPPLIER_TERMS_DISPLAY_VERSION_INVALID/);
  }
});

test('marked source facts reject missing routes, invented references, unsupported scope and misleading status', () => {
  for (const change of [
    product => { delete product.source_route_contract; },
    product => { product.source_route_contract = 'invented'; },
    product => { product.source_route_checks.pop(); },
    product => { product.source_route_checks.reverse(); },
    product => { product.source_route_checks[0].source = 'Invented market'; },
    product => { product.source_route_checks[0].source_role = 'supplier'; },
    product => { product.source_route_checks[0].description_hash = 'f'.repeat(64); },
    product => { product.source_route_checks[0].state = 'Relevant result found'; },
    product => { product.source_route_checks[0].actual_attempt = 1; },
    product => { product.source_route_checks[0].outcome = 'invented'; },
    product => { product.source_route_checks[0].evidence_ids = ['invented']; },
    product => { product.source_route_checks[0].task_ids = ['source-one']; },
    product => { product.source_route_checks[0].receipt_ids = ['source-one']; },
    product => { product.source_route_checks[0].detail = 'x'.repeat(8001); },
    product => { product.source_route_checks[0].observed_at = 'tomorrow'; },
    product => { product.source_route_checks[0].url = 'https://example.com/?api_key=fixture'; },
    product => { product.source_route_checks.find(row => row.route === 'google-trends').last_attempt.receipt_id = 'invented'; },
    product => { product.source_revisions.observations['source-one'] = 'not-a-hash'; },
  ]) {
    const p = sourcePacket(); change(p.facts.products[0]);
    assert.throws(() => validateFitPacket(p.facts, p.manifest), /ADMIN_SOURCE_ROUTE_/);
  }
  const historical = currentPacket();
  historical.facts.products[0].source_route_contract = 'saved-source-routes-v1';
  historical.facts.products[0].source_route_checks = [];
  assert.throws(() => validateFitPacket(historical.facts, historical.manifest), /ADMIN_SOURCE_ROUTE_/);
});

test('marked source numeric ranges reject rounding-dependent comparisons and ungrounded measurements', () => {
  for (const value of [true, -1, 1_000_000_001, '01.25', '1.250', '1e2', '+2', '0.0', '1'.repeat(33),
    '1000000000.0000000000000000001', ['2.5', '2.25'], [null, '2'], ['1.0000000000000000000002', '1.0000000000000000000001']]) {
    const p = sourcePacket(); p.facts.products[0].source_route_checks.find(row => row.route === 'google-trends').measurements[1].value = value;
    assert.throws(() => validateFitPacket(p.facts, p.manifest), /ADMIN_SOURCE_ROUTE_/);
  }
  for (const change of [
    metric => { metric.name = 'sales_guaranteed'; }, metric => { metric.evidence_ids = []; },
    metric => { metric.evidence_ids = ['receipt-one']; }, metric => { metric.value = 1.25; },
  ]) {
    const p = sourcePacket(); change(p.facts.products[0].source_route_checks.find(row => row.route === 'google-trends').measurements[1]);
    assert.throws(() => validateFitPacket(p.facts, p.manifest), /ADMIN_SOURCE_ROUTE_|ADMIN_NONINTEGER_JSON/);
  }
});

test('marked product photos require their own frozen product and original source receipt', () => {
  const p = sourcePacket(), product = p.facts.products[0];
  product.image_path = '/project/media/live/product-images/' + 'a'.repeat(64) + '.png';
  product.image_provenance = { id: 'receipt-one', owner_id: product.concept_id, path: product.image_path,
    sha256: 'a'.repeat(64), source_kind: 'public_product_image', source_ref: 'source-one',
    source_url: 'https://example.com/product.png', observed_at: BEFORE, relationship: 'exact_product' };
  assert.equal(validateFitPacket(p.facts, p.manifest), p.facts);
  for (const change of [
    product => { delete product.image_provenance; }, product => { product.image_provenance.owner_id = 'another-product'; },
    product => { product.image_provenance.source_kind = 'generated'; }, product => { product.image_provenance.path = '/wrong/photo.png'; },
    product => { product.image_provenance.id = 'invented'; }, product => { product.image_provenance.source_ref = 'invented'; },
    product => { product.image_provenance.source_ref = 'task-one'; }, product => { product.image_provenance.source_ref = 'receipt-one'; },
    product => { product.source_revisions.observations['unrelated-observation'] = 'a'.repeat(64);
      product.image_provenance.source_ref = 'unrelated-observation'; },
    product => { product.image_provenance.id = ['receipt-one']; },
    product => { product.image_provenance.sha256 = ['a'.repeat(64)]; },
    product => { product.image_provenance.source_ref = ['source-one']; },
    product => { product.image_provenance.sha256 = 'not-a-hash'; }, product => { product.image_provenance.relationship = 'unrelated'; },
    product => { product.image_provenance.source_url = 'https://example.com/?client_secret=fixture'; },
  ]) {
    const changed = structuredClone(p); change(changed.facts.products[0]);
    assert.throws(() => validateFitPacket(changed.facts, changed.manifest), /ADMIN_SOURCE_PHOTO_BINDING_INVALID/);
  }
});

test('marked source facts pass actual in-memory private upload and activation without changing permissions', async () => {
  const p = sourcePacket(), env = environment(); await upload(env, p);
  const response = await activate(env, p); assert.equal(response.status, 200, await response.text());
  assert.equal(JSON.parse(new TextDecoder().decode(env.REPORTS.objects.get(INDEX).bytes)).issues[0].product_count, 10);
});

test('distinct-platform v2 counts separate marketplaces and keeps all four priorities', () => {
  for (const [matches, sources, positive, priority] of [
    [6, ['amazon', 'flipkart'], [], 1], [5, ['blinkit', 'zepto'], [], 2],
    [6, ['amazon'], ['amazon'], 3], [5, ['google-trends'], ['google-trends'], 4],
    [6, ['meta', 'amazon', 'flipkart', 'google-trends', 'blinkit', 'zepto', 'instamart'], [], 1],
  ]) {
    const p = platformPacket(matches, sources, positive);
    assert.equal(p.facts.products[0].opportunity_priority, priority);
    assert.equal(validateFitPacket(p.facts, p.manifest), p.facts);
  }
});

test('platform counts cannot duplicate one site, suppliers or uncited evidence', () => {
  for (const mutate of [
    p => { p.facts.products[0].opportunity_basis.available_sources = true; },
    p => { p.facts.products[0].opportunity_basis.available_sources = 3; },
    p => { p.facts.products[0].opportunity_basis.source_ids = ['amazon', 'amazon']; },
    p => { p.facts.products[0].opportunity_basis.source_ids = ['amazon', 'supplier']; },
    p => { p.facts.products[0].opportunity_basis.positive_source_ids = ['meta']; },
    p => { p.facts.products[0].opportunity_basis.evidence_by_source.flipkart = []; },
    p => { p.facts.products[0].opportunity_basis.evidence_by_source.flipkart = ['source-amazon']; },
    p => { p.facts.products[0].opportunity_basis.evidence_by_source.flipkart = ['invented']; },
    p => { p.facts.products[0].source_refs = {}; },
    p => { p.facts.products[0].opportunity_policy_id = 'opportunity-priority-v1'; },
    p => { p.manifest.opportunity_policy_id = 'opportunity-priority-v1'; },
  ]) {
    const p = platformPacket(); mutate(p);
    assert.throws(() => validateFitPacket(p.facts, p.manifest), /ADMIN_OPPORTUNITY_/);
  }
  const unsupported = platformPacket(6, ['flipkart']);
  assert.throws(() => validateFitPacket(unsupported.facts, unsupported.manifest), /ADMIN_OPPORTUNITY_PRIORITY_INVALID/);
  const originalThreeGroup = currentPacket();
  originalThreeGroup.facts.opportunity_policy_id = originalThreeGroup.manifest.opportunity_policy_id = 'opportunity-priority-v1';
  for (const product of originalThreeGroup.facts.products) Object.assign(product, {
    opportunity_policy_id: 'opportunity-priority-v1', opportunity_priority: 1,
    opportunity_basis: { available_families: 4, positive_families: 0 },
  });
  assert.throws(() => validateFitPacket(originalThreeGroup.facts, originalThreeGroup.manifest), /ADMIN_OPPORTUNITY_BASIS_INVALID/);
});

test('current ten-point packets accept both five and six supported points without treating unknowns as failures', () => {
  for (const matches of [5, 6]) {
    const p = currentPacket(matches); assert.equal(validateFitPacket(p.facts, p.manifest), p.facts);
  }
});
test('current and historical packet contracts cannot mix or fabricate criterion references', () => {
  const changes = [p => { p.facts.products[0].fit_tier = 'good'; }, p => { p.facts.products[0].criteria[0].references = ['invented']; },
    p => { p.facts.products[0].criteria[0].id = 3; }, p => { p.manifest.kpi_total = 13; },
    p => { p.facts.products[0].kpi_matches = 6; }, p => { p.facts.products[0] = null; },
    p => { p.facts.products[0].source_refs.push({ id: 'source-one' }); }, p => { p.facts.schema_version = 1; }];
  for (const change of changes) {
    const p = currentPacket(); change(p); assert.throws(() => validateFitPacket(p.facts, p.manifest), /ADMIN_(?:FACT_REQUIREMENTS_NOT_MET|FIT_POLICY_MISMATCH)/);
  }
});
test('current report phrases must cover all ten points in each requested language', () => {
  for (const change of [p => p.facts.narratives.en.products[0].checklist_phrases.pop(),
    p => { p.facts.narratives.en.products[0].checklist_phrases[0].text = 'Saves 99 percent'; },
    p => { p.facts.narratives.en.products[0].checklist_phrases[0].text = 'Guaranteed profit'; },
    p => { p.facts.narratives['hi-Latn'].products[0].checklist_phrases[0].text = 'समय'; }]) {
    const p = currentPacket(); change(p); assert.throws(() => validateFitPacket(p.facts, p.manifest), /ADMIN_CHECKLIST_PHRASES_INVALID/);
  }
});
test('supplier-search effort cannot be claimed by a Boolean without frozen evidence', () => {
  for (const change of [p => { p.facts.products[0].supplier_effort.legacy_interpretation_ids = []; },
    p => { p.facts.products[0].supplier_effort.usable_lead_count = 1; },
    p => { p.facts.products[0].source_revisions.supplier_assessments = {}; },
    p => { p.facts.products[0].brand_binding.verdict = 'established'; }]) {
    const p = currentPacket(); change(p); assert.throws(() => validateFitPacket(p.facts, p.manifest), /ADMIN_FACT_REQUIREMENTS_NOT_MET/);
  }
});
test('current packet can pass the existing private upload and activation path without changing access controls', async () => {
  const p = currentPacket(), env = environment(); await upload(env, p);
  const response = await activate(env, p); assert.equal(response.status, 200, await response.text());
  assert.equal(JSON.parse(new TextDecoder().decode(env.REPORTS.objects.get(INDEX).bytes)).issues[0].product_count, 10);
});
test('supplier attempt URLs cannot carry secret query fields or encoded control characters', () => {
  const p = currentPacket(), product = p.facts.products[0];
  product.supplier_search_check_ids = ['effort-check'];
  product.source_revisions.source_checks = { 'effort-check': 'a'.repeat(64) };
  Object.assign(product.supplier_effort, { check_ids: ['effort-check'], internal_outcome: 'failed', effort_satisfied: true,
    last_attempt_at: BEFORE, actual_attempt_refs: [{ id: 'attempt-one', hash: 'e'.repeat(64), check_id: 'effort-check',
      pass_id: 'pass-one', reference: 'Saved fixture attempt', execution_stage: 'attempt_started', proof: 'owned_reader_execution',
      execution_ref: 'public-fetch-' + 'a'.repeat(32), attempt_started_at: BEFORE, checked_at: BEFORE,
      requested_url: 'https://example.com/search?q=organizer', final_url: null, http_status: null }] });
  assert.equal(validateFitPacket(p.facts, p.manifest), p.facts);
  for (const url of ['https://example.com/?api_key=fixture', 'https://example.com/?ACCESS_TOKEN=fixture',
    'https://example.com/search%0Atext', 'https://example.com/\\redirect', 'https://example.com/with space']) {
    const changed = structuredClone(p); changed.facts.products[0].supplier_effort.actual_attempt_refs[0].requested_url = url;
    assert.throws(() => validateFitPacket(changed.facts, changed.manifest), /ADMIN_FACT_REQUIREMENTS_NOT_MET/);
  }
});
test('a changed product description cannot reuse an older frozen brand judgment', async () => {
  const p = currentPacket(), env = environment();
  p.facts.products[0].description.function = 'A different fictional mechanism'; refreshFacts(p);
  await upload(env, p);
  const response = await activate(env, p);
  assert.equal(response.status, 409); assert.equal(await response.text(), 'ADMIN_BRAND_DESCRIPTION_CHANGED');
  assert.equal(env.REPORTS.objects.has(INDEX), false);
});
async function upload(env, p) {
  for (const [key, raw] of p.objects) {
    const match = /\/issues\/([^/]+)\/r(\d+)\/([^/]+)$/.exec(key), name = match[3];
    const type = name.endsWith('.pdf') ? 'application/pdf' : name.endsWith('.xlsx') ? 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' : 'application/json';
    const response = await handleAdminRequest(request(`/admin/objects/${match[1]}/${match[2]}/${name}`, 'PUT', raw, { 'Content-Type': type, 'X-Content-SHA256': sha(raw) }), env, context(), fixed(BEFORE));
    assert.equal(response.status, 200, await response.text());
  }
}
async function activate(env, p, { at = BEFORE, previous = null, etag = null, late = false, schedule = SCHEDULE, selected = null } = {}) {
  return handleAdminRequest(request('/admin/index', 'POST', encoded({ expected_etag: etag, mutation: activation(p, { previous, late, schedule, selected }) }), { 'Content-Type': 'application/json' }), env, context(), fixed(at));
}

test('signed-out, clients, forged headers and wrong audience cannot read or write admin storage', async () => {
  for (const ctx of [{}, context('client@example.com'), context('operator@example.com', 'wrong-aud')]) {
    const env = environment();
    const response = await handleAdminRequest(request('/admin/index', 'GET', undefined, { 'CF-Access-Authenticated-User-Email': 'operator@example.com', 'Cf-Access-Jwt-Assertion': 'forged' }), env, ctx, fixed(BEFORE));
    assert.equal(response.status, 403); assert.deepEqual(env.REPORTS.calls, []);
  }
});
test('operator and client allowlists must be distinct and missing configuration fails closed', async () => {
  for (const operators of [undefined, '["client@example.com"]', 'not-json']) {
    const env = environment(); env.OPERATOR_EMAILS = operators;
    assert.equal((await handleAdminRequest(request('/admin/index'), env, context(), fixed(BEFORE))).status, 503);
    assert.deepEqual(env.REPORTS.calls, []);
  }
});
test('owner-only setup permits operator checks without inventing client access', async () => {
  const env = environment(); env.ALLOWED_EMAILS = '[]';
  assert.equal((await handleRequest(request('/admin/index'), env, context())).status, 200);
  assert.equal((await handleRequest(request('/reports'), env, context())).status, 403);
  assert.equal((await handleRequest(request('/admin/index'), env, context('unknown@example.com'))).status, 403);
  const invalid = environment(); invalid.ALLOWED_EMAILS = '[]'; invalid.OPERATOR_EMAILS = '[]';
  assert.equal((await handleRequest(request('/admin/index'), invalid, context())).status, 503);
  assert.deepEqual(invalid.REPORTS.calls, []);
});

test('CSRF controls reject cross-origin, missing action header and preflight', async () => {
  const env = environment();
  for (const headers of [{ Origin: 'https://evil.example' }, { 'Sec-Fetch-Site': 'cross-site' }, { 'X-NeuraFlow-Release-Action': '' }]) {
    assert.equal((await handleAdminRequest(request('/admin/index', 'GET', undefined, headers), env, context(), fixed(BEFORE))).status, 403);
  }
  assert.equal((await handleAdminRequest(request('/admin/index', 'OPTIONS'), env, context(), fixed(BEFORE))).status, 405);
  assert.deepEqual(env.REPORTS.calls, []);
});
test('operator route is wired separately without granting operators ordinary client access', async () => {
  const env = environment();
  assert.equal((await handleRequest(request('/admin/index'), env, context())).status, 200);
  assert.equal((await handleRequest(request('/'), env, context())).status, 403);
  assert.equal((await handleRequest(request('/admin/index'), env, context('client@example.com'))).status, 403);
});
test('no arbitrary client keys, URLs, path traversal, filenames or deletion are accepted', async () => {
  const env = environment();
  for (const path of ['/admin/objects/other-client/1/secret.txt', '/admin/objects/issue/1/..%2Fsecret.pdf', '/admin/objects/issue/1/report-en.pdf?url=https://evil.example', '/admin/unknown']) {
    assert.ok([403, 404].includes((await handleAdminRequest(request(path), env, context(), fixed(BEFORE))).status));
  }
  assert.equal((await handleAdminRequest(request('/admin/objects/issue/1/report-en.pdf', 'DELETE'), env, context(), fixed(BEFORE))).status, 405);
  assert.deepEqual(env.REPORTS.calls, []);
});
test('bounded immutable uploads verify declared hash and cannot overwrite an existing object', async () => {
  const env = environment(), p = packet(); await upload(env, p);
  const key = [...p.objects.keys()].find(k => k.endsWith('report-en.pdf')), old = env.REPORTS.objects.get(key).bytes.slice();
  const different = enc.encode('%PDF-1.7\nChanged'), route = '/admin/objects/issue-one/1/report-en.pdf';
  const response = await handleAdminRequest(request(route, 'PUT', different, { 'Content-Type': 'application/pdf', 'X-Content-SHA256': sha(different) }), env, context(), fixed(BEFORE));
  assert.equal(response.status, 409); assert.deepEqual(env.REPORTS.objects.get(key).bytes, old);
  assert.equal((await handleAdminRequest(request(route, 'PUT', different, { 'Content-Type': 'application/pdf', 'X-Content-SHA256': 'a'.repeat(64) }), env, context(), fixed(BEFORE))).status, 409);
  assert.equal((await handleAdminRequest(request(route, 'PUT', different, { 'Content-Type': 'application/pdf', 'X-Content-SHA256': sha(different), 'Content-Length': String(33 * 1024 * 1024) }), env, context(), fixed(BEFORE))).status, 413);
});
test('stored object bounds prevent consuming oversized provider bodies', async () => {
  const env = environment(); let consumed = false;
  env.REPORTS.get = async () => ({ size: 33 * 1024 * 1024, arrayBuffer: async () => { consumed = true; return new ArrayBuffer(0); } });
  assert.equal((await handleAdminRequest(request('/admin/objects/issue/1/report-en.pdf'), env, context(), fixed(BEFORE))).status, 503);
  assert.equal(consumed, false);
});
test('files alone are private; activation requires stored package checks and real create-if-absent CAS', async () => {
  const env = environment(), p = packet(); await upload(env, p);
  assert.equal((await handleRequest(request('/download/issue-one/1/report-en.pdf'), env, context('client@example.com'), Date.parse(AFTER))).status, 404);
  const response = await activate(env, p); assert.equal(response.status, 200, await response.clone().text());
  const result = await response.json(); validateIndex(result.index, CLIENT);
  assert.equal(result.index.issues[0].server_activated_at, BEFORE);
  const call = env.REPORTS.calls.find(v => v[0] === 'put' && v[1] === INDEX);
  assert.equal(call[2].onlyIf.get('If-None-Match'), '*');
  assert.equal((await handleRequest(request('/download/issue-one/1/report-en.pdf'), env, context('client@example.com'), Date.parse(BEFORE))).status, 404);
  assert.equal((await handleRequest(request('/download/issue-one/1/report-en.pdf'), env, context('client@example.com'), Date.parse(AFTER))).status, 200);
});
test('stale ETag or final CAS conflict leaves orphan uploads inaccessible', async () => {
  for (const stale of [true, false]) {
    const env = environment(), p = packet(); await upload(env, p); env.REPORTS.conflict = !stale;
    const response = await activate(env, p, { etag: stale ? 'invented-prior-etag' : null });
    assert.equal(response.status, 409); assert.equal(env.REPORTS.objects.has(INDEX), false);
    assert.equal((await handleRequest(request('/download/issue-one/1/report-en.pdf'), env, context('client@example.com'), Date.parse(AFTER))).status, 404);
  }
});
test('missing or edited stored PDFs and facts cannot be activated by an approval claim', async () => {
  for (const name of ['report-hi-Latn.pdf', 'final_report_data.json']) {
    const env = environment(), p = packet(); await upload(env, p);
    env.REPORTS.objects.get([...p.objects.keys()].find(k => k.endsWith(name))).bytes = enc.encode('{}');
    assert.ok([409, 503].includes((await activate(env, p)).status)); assert.equal(env.REPORTS.objects.has(INDEX), false);
  }
});
test('synthetic packages and model-like approval omissions are rejected', async () => {
  const env = environment(), p = packet(); await upload(env, p);
  for (const edit of [m => { m.allow_synthetic = true; }, m => { m.approval.approved = false; }, m => { m.approval.package_hash = 'f'.repeat(64); }]) {
    const mutation = activation(p); edit(mutation);
    const response = await handleAdminRequest(request('/admin/index', 'POST', encoded({ expected_etag: null, mutation }), { 'Content-Type': 'application/json' }), env, context(), fixed(BEFORE));
    assert.ok([400, 403].includes(response.status)); assert.equal(env.REPORTS.objects.has(INDEX), false);
  }
});
test('configured owner morning, calendar and language are required rather than invented', async () => {
  const env = environment(), p = packet(); await upload(env, p); env.PUBLICATION_TIME = '';
  assert.equal((await activate(env, p)).status, 400); assert.equal(env.REPORTS.objects.has(INDEX), false);
});
test('configured morning means the exact instant, not any second within that minute', async () => {
  const env = environment(), p = packet(); await upload(env, p);
  for (const schedule of ['2026-10-13T02:30:01+00:00', '2026-10-13T02:30:00.001Z']) {
    assert.equal((await activate(env, p, { schedule })).status, 400);
    assert.equal(env.REPORTS.objects.has(INDEX), false);
  }
});
test('a previously published concept needs a reviewed material change before being called Updated', async () => {
  for (const supported of [false, true]) {
    const env = environment(), first = packet(); await upload(env, first);
    const prior = await (await activate(env, first)).json();
    const next = packet('issue-two', 1, 2, '2026-10-15');
    next.facts.products[0].concept_id = first.facts.products[0].concept_id;
    next.facts.products[0].status = 'Updated';
    if (supported) Object.assign(next.facts.products[0], { material_update_reviewed: true, update_reason: 'Manufactured fixture: reviewed new supplier support.' });
    const m = next.manifest, rawFacts = encoded(next.facts);
    next.objects.set(`clients/${CLIENT}/issues/issue-two/r1/final_report_data.json`, rawFacts);
    Object.assign(m.artifacts.facts, { sha256: sha(rawFacts), size: rawFacts.length });
    m.facts_hash = sha(enc.encode(canonical(next.facts)));
    m.concept_ids = next.facts.products.map(product => product.concept_id);
    m.new_count = 9; m.counts = { products: 10, new: 9, updates: 1 };
    const unsigned = { ...m }; delete unsigned.package_hash;
    m.package_hash = sha(enc.encode(canonical(unsigned)));
    next.objects.set(`clients/${CLIENT}/issues/issue-two/r1/delivery-manifest.json`, encoded(m));
    await upload(env, next);
    const result = await activate(env, next, { at: AFTER, etag: prior.etag, schedule: '2026-10-15T02:30:00+00:00' });
    assert.equal(result.status, supported ? 200 : 409, await result.clone().text());
    if (!supported) assert.equal(env.REPORTS.objects.get(INDEX).etag, prior.etag);
  }
});
test('private release history binds the trusted Access actor separately from caller approval labels', async () => {
  const env = environment(), p = packet(); await upload(env, p);
  const accepted = await (await activate(env, p)).json();
  assert.equal(accepted.index.issues[0].owner_id, 'owner-one');
  assert.equal(accepted.index.issues[0].server_actor_email, 'operator@example.com');
  const mutation = { kind: 'withdraw', client_id: CLIENT, issue_id: 'issue-one', revision: 1, package_hash: p.manifest.package_hash,
    approval: { approved: true, owner_id: 'different-caller-label', client_id: CLIENT, issue_id: 'issue-one', revision: 1, package_hash: p.manifest.package_hash, action: 'withdraw' },
    server_deactivated_by: 'forged@example.com' };
  const response = await handleAdminRequest(request('/admin/index', 'POST', encoded({ expected_etag: accepted.etag, mutation }), { 'Content-Type': 'application/json' }), env, context(), fixed(BEFORE));
  assert.equal(response.status, 200, await response.clone().text());
  const result = await response.json();
  assert.equal(result.index.issues[0].server_deactivated_by, 'operator@example.com');
  assert.equal(result.index.issues[0].server_actor_email, 'operator@example.com');
});
test('only the first two logical issues are bilingual; later releases require the chosen language', async () => {
  const env = environment(), first = packet(); await upload(env, first);
  let accepted = await (await activate(env, first)).json();
  const second = packet('issue-two', 1, 2, '2026-10-15'); await upload(env, second);
  accepted = await (await activate(env, second, { etag: accepted.etag, schedule: '2026-10-15T02:30:00+00:00' })).json();
  const third = packet('issue-three', 1, 3, '2026-10-17'); await upload(env, third);
  const options = { etag: accepted.etag, schedule: '2026-10-17T02:30:00+00:00', selected: 'en' };
  assert.equal((await activate(env, third, options)).status, 400);
  env.ONGOING_LANGUAGE = 'hi-Latn';
  assert.equal((await activate(env, third, options)).status, 400);
  assert.equal(env.REPORTS.objects.get(INDEX).etag, accepted.etag);
  env.ONGOING_LANGUAGE = 'en';
  const response = await activate(env, third, options);
  assert.equal(response.status, 200, await response.clone().text());
  const result = await response.json();
  assert.deepEqual(result.index.issues.map(entry => entry.languages), [['en', 'hi-Latn'], ['en', 'hi-Latn'], ['en']]);
  assert.deepEqual(result.index.issues.map(entry => entry.publication_ordinal), [1, 2, 3]);
});
test('failed replacement retains previous report visibility and immutable bytes', async () => {
  const env = environment(), first = packet(); await upload(env, first);
  const accepted = await (await activate(env, first)).json(), old = env.REPORTS.objects.get(INDEX).bytes.slice();
  const replacement = packet('issue-one', 2); await upload(env, replacement); env.REPORTS.failIndex = true;
  const response = await activate(env, replacement, { at: AFTER, etag: accepted.etag, late: true, previous: { revision: 1, package_hash: first.manifest.package_hash } });
  assert.equal(response.status, 503); assert.doesNotMatch(await response.text(), /private-provider-message/); assert.deepEqual(env.REPORTS.objects.get(INDEX).bytes, old);
  assert.equal((await handleRequest(request('/download/issue-one/1/report-en.pdf'), env, context('client@example.com'), Date.parse(AFTER))).status, 200);
});
test('successful revisions use actual ETag and preserve published withdrawal intervals', async () => {
  const env = environment(), first = packet(); await upload(env, first);
  const accepted = await (await activate(env, first)).json(), replacement = packet('issue-one', 2); await upload(env, replacement);
  const response = await activate(env, replacement, { at: AFTER, etag: accepted.etag, late: true, previous: { revision: 1, package_hash: first.manifest.package_hash } });
  assert.equal(response.status, 200, await response.clone().text()); const updated = await response.json();
  assert.equal(updated.index.issues[0].remote_state, 'withdrawn'); assert.equal(updated.index.issues[0].server_deactivated_at, AFTER);
  assert.equal(updated.index.issues[1].publication_ordinal, 1);
  assert.equal(projectOrdinal(updated.index, 'second', '2026-10-15T02:30:00+00:00', AFTER), 2);
  assert.equal(env.REPORTS.calls.filter(c => c[0] === 'put' && c[1] === INDEX).at(-1)[2].onlyIf.etagMatches, accepted.etag);
});
test('replayed approval does not change activation time or create a revision', async () => {
  const env = environment(), p = packet(); await upload(env, p);
  const accepted = await (await activate(env, p)).json();
  const response = await activate(env, p, { at: AFTER, etag: accepted.etag });
  assert.equal(response.status, 200); const repeated = await response.json();
  assert.equal(repeated.etag, accepted.etag); assert.equal(repeated.index.issues.length, 1); assert.equal(repeated.index.issues[0].server_activated_at, BEFORE);
});
test('queue cancellation only withdraws affected never-released ordinals; files remain intact', async () => {
  const env = environment(), first = packet(); await upload(env, first); let accepted = await (await activate(env, first)).json();
  const second = packet('issue-two', 1, 2, '2026-10-15'); await upload(env, second);
  accepted = await (await activate(env, second, { etag: accepted.etag, schedule: '2026-10-15T02:30:00+00:00' })).json();
  const mutation = { kind: 'withdraw', client_id: CLIENT, issue_id: 'issue-one', revision: 1, package_hash: first.manifest.package_hash,
    approval: { approved: true, owner_id: 'owner-one', client_id: CLIENT, issue_id: 'issue-one', revision: 1, package_hash: first.manifest.package_hash, action: 'withdraw' } };
  const response = await handleAdminRequest(request('/admin/index', 'POST', encoded({ expected_etag: accepted.etag, mutation }), { 'Content-Type': 'application/json' }), env, context(), fixed(BEFORE));
  assert.equal(response.status, 200, await response.clone().text()); const result = await response.json();
  assert.equal(result.index.issues[1].withdrawal_reason, 'queue_reprojection_required'); assert.equal(env.REPORTS.objects.size, 11);
});
test('JavaScript canonical encoding matches frozen actual Python canonical hash for packet values', () => {
  const value = { money: '149.00', label: '₹149 · NeuraFlow sujhaav', values: [true, null, 10], '\u0001': 'escaped' };
  // Independently produced with product_radar.models.digest in the repository
  // Python environment; do not derive expected values using this JS encoder.
  assert.equal(sha(enc.encode(canonical(value))), 'ad05e560a0ae0f3993f616e02fb47f907b6804cb2115e21b80f3fb130ac16fbe');
});
test('ambiguous JSON, invalid identity lookup and malformed request bodies never reach storage writes', async () => {
  for (const payload of ['{"expected_etag":null,"expected_etag":"forged","mutation":{}}', '{malformed', '[]']) {
    const env = environment();
    const response = await handleAdminRequest(request('/admin/index', 'POST', enc.encode(payload), { 'Content-Type': 'application/json' }), env, context(), fixed(BEFORE));
    assert.equal(response.status, 400); assert.equal(env.REPORTS.calls.some(c => c[0] === 'put'), false);
  }
  const env = environment(), ctx = context(); ctx.access.getIdentity = async () => { throw new Error('private auth text'); };
  const response = await handleAdminRequest(request('/admin/index'), env, ctx, fixed(BEFORE));
  assert.equal(response.status, 403); assert.doesNotMatch(await response.text(), /private auth/); assert.deepEqual(env.REPORTS.calls, []);
});
