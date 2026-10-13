// Private operator bridge. No deployment, Access application or account is configured here.
// R2 conditions: https://developers.cloudflare.com/r2/api/workers/workers-api-reference/
// Access identity: https://developers.cloudflare.com/workers/configuration/cloudflare-access/
// Named operator identity is required. A ChatGPT/browser sign-in is not a machine credential.
import { createServiceAssertionVerifier, ServiceAssertionError } from './access-service-assertion.mjs';
import { validateFunctionalPacket as verifyFunctionalPacket } from './functional-packet.mjs';
import { ReleaseTree, ReleasePageError, PAGE_LIMIT, ROOT_LIMIT, validateReleaseRoot, releaseBytes, releaseDigest,
  releaseSequenceKey, releaseQueueKey, releaseHoldKey, releaseHash, parseRelease, boundedReleaseBytes, releaseCursor, readReleaseCursor } from './release-pages.mjs';
import { buildReleaseHistory, archiveEntry, issueProjection, selectedTypeHistory, settleReleaseQueue,
  firstAvailability, publicationRef, recommendationsFromFacts, timelineKey, revisionKey, revisionProjection, canonicalType, lineageKey, lineageTypeContext, validateLineageAdjacency } from './release-history.mjs';
import { validateAliasProof } from './alias-proof.mjs';
import { validateLineageProof } from './lineage-proof.mjs';
const verifyDraftServiceAssertion = createServiceAssertionVerifier();
const ID = /^[A-Za-z0-9_-]{1,80}$/;
const HASH = /^[a-f0-9]{64}$/;
const INDEX_LIMIT = 512 * 1024;
const FACTS_LIMIT = 4 * 1024 * 1024;
const FILE_LIMIT = 32 * 1024 * 1024;
const TYPES = {
  'shortlist.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'report-en.pdf': 'application/pdf', 'report-hi-Latn.pdf': 'application/pdf',
  'final_report_data.json': 'application/json', 'delivery-manifest.json': 'application/json',
};
const encoder = new TextEncoder();
const FIT_IDS = Object.freeze([1, 2, 5, 6, 7, 8, 9, 11, 12, 13]);
const FIT_STATES = Object.freeze(['Meets', 'Does not meet', 'Unclear', 'Not assessed']);
const FIT_DESCRIPTION_CONTRACT = 'product-description-inference-v1';
const FIT_DESCRIPTION_FIELDS = new Set(['label', 'function', 'mechanism', 'features', 'audience']);
const INFERRED_MEASUREMENT = /(?:\p{Nd}+(?:[.,]\p{Nd}+)?\s*(?:%|percent\b|per\s+cent\b|kg\b|kilograms?\b|grams?\b|cm\b|centimet(?:er|re)s?\b|mm\b|millimet(?:er|re)s?\b|met(?:er|re)s?\b|inches\b|lbs?\b|pounds?\b|minutes?\b|mins?\b|seconds?\b|secs?\b|hours?\b|rupees?\b|dollars?\b|sales\b|orders\b)|[₹$€£]\s*\p{Nd})/iu;
const OPPORTUNITY_POLICY = 'opportunity-priority-v1';
const OPPORTUNITY_PLATFORM_POLICY = 'opportunity-priority-v2';
const OPPORTUNITY_POLICIES = Object.freeze([OPPORTUNITY_POLICY, OPPORTUNITY_PLATFORM_POLICY]);
const OPPORTUNITY_SOURCES = Object.freeze(['meta', 'amazon', 'flipkart', 'google-trends', 'blinkit', 'zepto', 'instamart']);
const OPPORTUNITY_FIELDS = Object.freeze(['opportunity_policy_id', 'opportunity_priority', 'opportunity_basis']);
const SOURCE_ROUTE_CONTRACT = 'saved-source-routes-v1';
const SUPPLIER_TERMS_DISPLAY_VERSION = 'supplier-terms-v2';
const SOURCE_ROUTES = Object.freeze([
  ['meta-ads', 'Facebook Ads Library', 'meta'],
  ['amazon-bestsellers', 'Amazon India Bestsellers', 'marketplace'],
  ['amazon-movers-and-shakers', 'Amazon India Movers & Shakers', 'marketplace'],
  ['amazon-search', 'Amazon India product search', 'marketplace'],
  ['flipkart-search', 'Flipkart product search', 'marketplace'],
  ['google-trends', 'Google Trends India', 'trends'],
  ['blinkit', 'Blinkit', 'quickcommerce'], ['zepto', 'Zepto', 'quickcommerce'], ['instamart', 'Instamart', 'quickcommerce'],
  ['google-suppliers', 'Google supplier search', 'supplier'], ['google-maps', 'Google Maps supplier search', 'supplier'],
  ['indiamart', 'IndiaMART', 'supplier'], ['tradeindia', 'TradeIndia', 'supplier'], ['company-pages', 'Supplier company pages', 'supplier'],
]);
const SOURCE_MEASUREMENTS = new Set(['relevant_ads', 'advertisers', 'active_ads', 'unknown_active_status', 'missing_advertiser_ids',
  'relevant_listings', 'list_rank_bounds', 'displayed_prices', 'available_listings', 'unavailable_listings', 'unknown_availability',
  'chart_intervals', 'nonzero_points', 'previous_mean_bounds', 'recent_mean_bounds']);
const SOURCE_REFERENCE_TABLES = Object.freeze(['query_tasks', 'source_checks', 'observations', 'captures', 'trends_evidence']);
const SOURCE_LAST_OUTCOMES = new Set(['usable', 'partial', 'interpretation_pending', 'empty', 'completed_no_results',
  'insufficient', 'blocked', 'failed', 'unavailable']);
const SECRET_URL_FIELDS = new Set(['token', 'access_token', 'api_token', 'api_key', 'apikey', 'password', 'secret',
  'client_secret', 'session_token', 'authorization']);
const decoder = new TextDecoder('utf-8', { fatal: true });
class Failure extends Error { constructor(code, status = 400) { super(code); this.status = status; } }
const fail = (code, status) => { throw new Failure(code, status); };
const hash = async bytes => [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(v => v.toString(16).padStart(2, '0')).join('');
const prefix = (client, issue, revision) => `clients/${client}/issues/${issue}/r${revision}/`;
const integer = (value, min, max = Number.MAX_SAFE_INTEGER) => Number.isSafeInteger(value) && value >= min && value <= max;

// This packet contract uses integers and decimal strings, not floating amounts.
// Exact sorted UTF-8 JSON matches product_radar.models.canonical for that contract.
export function canonical(value) {
  if (value === null || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'number') {
    if (!Number.isSafeInteger(value)) fail('ADMIN_NONINTEGER_JSON');
    return JSON.stringify(value);
  }
  if (typeof value === 'string') {
    if (/[\uD800-\uDFFF]/u.test(value)) fail('ADMIN_JSON_UNICODE_INVALID');
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (typeof value !== 'object') fail('ADMIN_JSON_INVALID');
  const keys = Object.keys(value).sort((a, b) => {
    const left = Array.from(a, v => v.codePointAt(0)), right = Array.from(b, v => v.codePointAt(0));
    for (let i = 0; i < Math.min(left.length, right.length); i++) if (left[i] !== right[i]) return left[i] - right[i];
    return left.length - right.length;
  });
  return '{' + keys.map(key => canonical(key) + ':' + canonical(value[key])).join(',') + '}';
}
const same = (a, b) => canonical(a) === canonical(b);
const jsonBytes = value => encoder.encode(canonical(value) + '\n');

function descriptionInferenceValid(product, row) {
  const inference = row.description_inference;
  if (inference == null) return false;
  const populated = field => {
    const value = product.description?.[field];
    const text = item => typeof item === 'string' && item.trim() && Array.from(item).length <= 2000;
    return field === 'features' ? Array.isArray(value) && value.length > 0 && value.length <= 100 && value.every(text) : !!text(value);
  };
  if (product.fit_description_contract !== FIT_DESCRIPTION_CONTRACT || !inference || typeof inference !== 'object' || Array.isArray(inference) ||
      !same(Object.keys(inference).sort(), ['description_hash', 'fields', 'kind']) || inference.kind !== FIT_DESCRIPTION_CONTRACT ||
      typeof inference.description_hash !== 'string' || !HASH.test(inference.description_hash) ||
      inference.description_hash !== product.brand_binding?.description_hash || !Array.isArray(inference.fields) || !inference.fields.length ||
      new Set(inference.fields).size !== inference.fields.length || inference.fields.some(field => typeof field !== 'string' ||
        !FIT_DESCRIPTION_FIELDS.has(field) || !Object.hasOwn(product.description ?? {}, field) || !populated(field)) ||
      !FIT_IDS.includes(row.id) || !['Meets', 'Does not meet'].includes(row.state) || row.basis !== 'inferred' ||
      !Array.isArray(row.references) || row.references.length !== 0 || typeof row.reason !== 'string' ||
      !row.reason.startsWith('Estimated:') || !row.reason.slice('Estimated:'.length).trim() || Array.from(row.reason).length > 2000 ||
      INFERRED_MEASUREMENT.test(row.reason)) fail('ADMIN_FIT_DESCRIPTION_INFERENCE_INVALID', 409);
  return true;
}

function supplierEffortValid(product) {
  const effort = product.supplier_effort, revisions = product.source_revisions;
  if (!effort || !revisions || product.supplier_effort_satisfied !== true) return false;
  const ids = value => Array.isArray(value) && value.every(id => typeof id === 'string' && ID.test(id)) && new Set(value).size === value.length;
  const bound = (table, values) => revisions[table] && typeof revisions[table] === 'object' &&
    Object.entries(revisions[table]).every(([id, digest]) => ID.test(id) && typeof digest === 'string' && HASH.test(digest)) && values.every(id => Object.hasOwn(revisions[table], id));
  const checks = effort.check_ids, legacy = effort.legacy_interpretation_ids ?? [], searches = effort.legacy_search_check_ids ?? [];
  if (![checks, legacy, searches, product.supplier_assessment_ids, product.supplier_search_check_ids].every(ids) ||
      !same([...legacy].sort(), [...product.supplier_assessment_ids].sort()) ||
      legacy.length && !bound('supplier_assessments', legacy) || checks.some(id => searches.includes(id)) ||
      !same([...checks, ...searches].sort(), [...product.supplier_search_check_ids].sort()) ||
      product.supplier_search_check_ids.length && !bound('source_checks', product.supplier_search_check_ids)) return false;
  const leads = product.suppliers;
  const scoped = product.recommendation_contract === 'functional-type-examples-v1' &&
    product.functional_supplier_proof?.contract === 'functional-supplier-scoped-proof-v2';
  if (!Array.isArray(leads) || leads.some(lead => !lead || !ID.test(lead.lead_id ?? '')) ||
      new Set(leads.map(lead => lead.lead_id)).size !== leads.length || !integer(effort.usable_lead_count, 0) ||
      effort.usable_lead_count !== leads.length || product.supplier_leads_found !== leads.length ||
      (scoped ? !ids(effort.scoped_interpretation_ids) || !bound('supplier_assessments', effort.scoped_interpretation_ids) ||
        leads.length > legacy.length + (product.supplier_scope_coverage?.selected_groups ?? -1) : leads.length > legacy.length)) return false;
  const outcomes = ['not_attempted', 'usable_leads', 'completed_empty', 'blocked', 'failed', 'partial', 'interpretation_pending', 'prelaunch_failed'];
  if (!outcomes.includes(effort.internal_outcome) || (leads.length > 0) !== (effort.internal_outcome === 'usable_leads') ||
      !['attempted', 'complete', 'blocked'].includes(product.supplier_search_status) || !Array.isArray(effort.actual_attempt_refs) ||
      effort.effort_satisfied !== (effort.actual_attempt_refs.length > 0)) return false;
  const seen = new Set(), dates = [];
  for (const ref of effort.actual_attempt_refs) {
    if (!ref || !checks.includes(ref.check_id) || !ID.test(ref.id ?? '') || !ID.test(ref.pass_id ?? '') ||
        !HASH.test(ref.hash ?? '') || typeof ref.reference !== 'string' || !ref.reference || ref.reference.length > 4096 ||
        !['attempt_started', 'response_received'].includes(ref.execution_stage)) return false;
    const identity = ref.id + ':' + ref.hash;
    if (seen.has(identity)) return false; seen.add(identity);
    const start = utc(ref.attempt_started_at), checked = utc(ref.checked_at);
    if (start > checked) return false; dates.push(ref.attempt_started_at);
    if (ref.proof === 'owned_reader_execution') {
      if (!/^(?:public-fetch|supplier-browser)-[a-f0-9]{32}$/.test(ref.execution_ref ?? '') ||
          ref.execution_stage === 'response_received' && !integer(ref.http_status, 100, 599)) return false;
    } else if (ref.proof === 'legacy_http_response') {
      if (ref.execution_stage !== 'response_received' || ref.execution_ref !== null || !ref.final_url || !integer(ref.http_status, 100, 599)) return false;
    } else return false;
    for (const value of [ref.requested_url, ...(ref.final_url == null ? [] : [ref.final_url])]) {
      try {
        if (typeof value !== 'string' || !value || value.length > 4096 || /[\x00-\x20\x7f\\]/.test(value) ||
            /[\x00-\x1f\x7f]/.test(decodeURIComponent(value))) return false;
        const url = new URL(value);
        if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password || url.hostname.includes('%') ||
            [...url.searchParams.keys()].some(name => SECRET_URL_FIELDS.has(name.toLowerCase()))) return false;
      }
      catch { return false; }
    }
  }
  const last = dates.length ? dates.reduce((a, b) => utc(a) >= utc(b) ? a : b) : null;
  return effort.last_attempt_at === last && (!dates.length || effort.internal_outcome !== 'not_attempted') && Boolean(dates.length || legacy.length || searches.length);
}

function sourceOutcomeLabel(outcome, attempted) {
  const labels = { not_attempted: 'Not tried', not_connected: 'Not tried', not_applicable: 'Not applicable',
    completed_relevant: 'Relevant result found', completed_no_results: 'No usable result found',
    interpretation_pending: 'Results need review', insufficient: 'Result unclear',
    in_progress: attempted ? 'Check in progress' : 'Not tried',
    failed: attempted ? 'Check failed' : 'Not tried', blocked: attempted ? 'Check blocked' : 'Not tried' };
  return Object.hasOwn(labels, outcome) ? labels[outcome] : null;
}

function publicFactUrl(value) {
  if (typeof value !== 'string' || !value || value.length > 4096 || /[\x00-\x20\x7f\\]/.test(value)) return false;
  try {
    if (/[\x00-\x1f\x7f]/.test(decodeURIComponent(value))) return false;
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && Boolean(url.hostname) && !url.username && !url.password &&
      !url.hostname.includes('%') && ![...url.searchParams.keys()].some(name => SECRET_URL_FIELDS.has(name.toLowerCase()));
  } catch { return false; }
}

// New measurements keep integer counts and encode calculated fractional values
// as plain decimal strings. Never relax canonical(): Python and JavaScript
// serialize floating-point numbers differently, which would break facts hashes.
function metricNumberText(value) {
  if (integer(value, 0, 1_000_000_000)) return String(value);
  if (typeof value !== 'string' || value.length > 32 || !/^(?:0|[1-9]\d*)(?:\.\d*[1-9])?$/.test(value)) return null;
  const [whole, fraction = ''] = value.split('.');
  if (whole.length > 10 || whole.length === 10 && (whole !== '1000000000' || fraction)) return null;
  return value;
}

function orderedMetricNumbers(left, right) {
  const [leftWhole, leftFraction = ''] = left.split('.'), [rightWhole, rightFraction = ''] = right.split('.');
  if (leftWhole.length !== rightWhole.length) return leftWhole.length < rightWhole.length;
  if (leftWhole !== rightWhole) return leftWhole < rightWhole;
  const size = Math.max(leftFraction.length, rightFraction.length);
  return leftFraction.padEnd(size, '0') <= rightFraction.padEnd(size, '0');
}

function validateSourceRouteFacts(product, current) {
  const marked = Object.hasOwn(product, 'source_route_contract') || Object.hasOwn(product, 'source_route_checks');
  if (!marked) return; // Frozen historical packets keep their original contract.
  const invalid = () => fail('ADMIN_SOURCE_ROUTE_FACTS_INVALID', 409);
  const rows = product.source_route_checks, revisions = product.source_revisions;
  if (!current || product.opportunity_policy_id !== OPPORTUNITY_PLATFORM_POLICY ||
      product.source_route_contract !== (product.recommendation_contract === 'functional-type-examples-v1' ? 'saved-source-routes-v2' : SOURCE_ROUTE_CONTRACT) || !Array.isArray(rows) || rows.length !== SOURCE_ROUTES.length ||
      !revisions || typeof revisions !== 'object' || Array.isArray(revisions) || encoder.encode(canonical(rows)).length > 64 * 1024) invalid();
  const known = new Set();
  for (const table of SOURCE_REFERENCE_TABLES) {
    const values = revisions[table] ?? {};
    if (!values || typeof values !== 'object' || Array.isArray(values) ||
        Object.entries(values).some(([id, digest]) => !ID.test(id) || typeof digest !== 'string' || !HASH.test(digest))) invalid();
    for (const ref of Object.keys(values)) known.add(ref);
  }
  const refsValid = (values, admitted) => Array.isArray(values) && values.every(ref => typeof ref === 'string' && ID.test(ref) && admitted.has(ref)) &&
    new Set(values).size === values.length;
  const dateValid = value => { try { utc(value); return true; } catch { return false; } };
  for (const [index, row] of rows.entries()) {
    const [route, label, role] = SOURCE_ROUTES[index];
    if (product.recommendation_contract === 'functional-type-examples-v1' &&
        (row?.meaning_hash !== product.functional_definition?.meaning_hash || !Array.isArray(row?.scoped_evidence_ids) ||
         row.scoped_evidence_ids.some(id => !Object.hasOwn(revisions.source_checks ?? {}, id)))) invalid();
    if (!row || typeof row !== 'object' || Array.isArray(row) || row.route !== route || row.source !== label || row.source_role !== role ||
        row.description_hash !== product.brand_binding?.description_hash || typeof row.actual_attempt !== 'boolean' ||
        typeof row.outcome !== 'string' || sourceOutcomeLabel(row.outcome, row.actual_attempt) === null ||
        row.state !== sourceOutcomeLabel(row.outcome, row.actual_attempt) || typeof row.detail !== 'string' || !row.detail || row.detail.length > 8000 ||
        !refsValid(row.evidence_ids, known) || !refsValid(row.task_ids, new Set(Object.keys(revisions.query_tasks ?? {}))) ||
        !refsValid(row.receipt_ids, new Set(Object.keys(revisions.source_checks ?? {}))) ||
        row.observed_at !== null && !dateValid(row.observed_at) || row.url !== null && !publicFactUrl(row.url) ||
        !Array.isArray(row.measurements) || row.measurements.length > SOURCE_MEASUREMENTS.size) invalid();
    const measured = new Set();
    for (const metric of row.measurements) {
      if (!metric || typeof metric !== 'object' || Array.isArray(metric) ||
          !same(Object.keys(metric).sort(), ['evidence_ids', 'name', 'value']) || !SOURCE_MEASUREMENTS.has(metric.name) ||
          measured.has(metric.name) || !refsValid(metric.evidence_ids, new Set(row.evidence_ids)) || !metric.evidence_ids.length) invalid();
      measured.add(metric.name);
      if (Array.isArray(metric.value)) {
        const values = metric.value.map(metricNumberText);
        if (values.length !== 2 || values.some(value => value === null) || !orderedMetricNumbers(values[0], values[1])) invalid();
      } else if (metric.value !== null && metricNumberText(metric.value) === null) invalid();
    }
    if (row.last_attempt !== null) {
      const last = row.last_attempt;
      if (!last || typeof last !== 'object' || Array.isArray(last) || !same(Object.keys(last).sort(), ['actual_attempt', 'at', 'outcome', 'receipt_id']) ||
          !SOURCE_LAST_OUTCOMES.has(last.outcome) || typeof last.actual_attempt !== 'boolean' ||
          !row.receipt_ids.includes(last.receipt_id) || !dateValid(last.at)) invalid();
    }
  }
  const photo = product.image_provenance;
  if (photo == null) {
    if (product.image_path != null) fail('ADMIN_SOURCE_PHOTO_BINDING_INVALID', 409);
  } else if (!photo || typeof photo !== 'object' || Array.isArray(photo) || photo.owner_id !== product.concept_id ||
      photo.source_kind !== 'public_product_image' || !['exact_product', 'relevant_family'].includes(photo.relationship) ||
      typeof photo.path !== 'string' || !photo.path || photo.path !== product.image_path || photo.path.length > 4096 ||
      /[\x00-\x1f\x7f]/.test(photo.path) || typeof photo.sha256 !== 'string' || !HASH.test(photo.sha256) ||
      typeof photo.id !== 'string' || !ID.test(photo.id) || typeof photo.source_ref !== 'string' || !ID.test(photo.source_ref) ||
      !Object.hasOwn(revisions.source_checks ?? {}, photo.id) || !Object.hasOwn(revisions.observations ?? {}, photo.source_ref) ||
      !Array.isArray(product.source_refs) || !product.source_refs.some(ref => ref?.id === photo.source_ref) ||
      !publicFactUrl(photo.source_url) || !dateValid(photo.observed_at)) fail('ADMIN_SOURCE_PHOTO_BINDING_INVALID', 409);
}

// Pure packet validation. Historical13/7 and declared10/5 are separate
// contracts; neither an array length nor a model-supplied total selects rules.
export function validateFitPacket(facts, manifest) {
  const functional = facts?.schema_version === 3, current = functional || facts?.schema_version === 2;
  if (!facts || !manifest || typeof manifest !== 'object' || ![1, 2, 3].includes(facts.schema_version) || !Array.isArray(facts.products)) fail('ADMIN_FACT_REQUIREMENTS_NOT_MET', 409);
  if (functional && (facts.recommendation_contract !== 'functional-type-examples-v1' || manifest.schema_version !== 2 ||
      manifest.recommendation_contract !== facts.recommendation_contract || facts.products.some(p => p?.recommendation_contract !== facts.recommendation_contract))) fail('ADMIN_FUNCTIONAL_TUPLE_INVALID', 409);
  if (!functional && (Object.hasOwn(facts, 'recommendation_contract') || Object.hasOwn(manifest, 'recommendation_contract') ||
      facts.products.some(p => p && Object.hasOwn(p, 'recommendation_contract')))) fail('ADMIN_FUNCTIONAL_TUPLE_INVALID', 409);
  const fields = ['fit_policy_id', 'criterion_ids', 'kpi_total', 'workbook_contract'];
  if (current) {
    if (facts.fit_policy_id !== 'fit-10-v1' || !same(facts.criterion_ids, FIT_IDS) || facts.kpi_total !== 10 ||
        manifest.fit_policy_id !== 'fit-10-v1' || !same(manifest.criterion_ids, FIT_IDS) || manifest.kpi_total !== 10 ||
        manifest.workbook_contract !== (functional ? 'fit-checklist-examples-v2' : 'fit-checklist-v1')) fail('ADMIN_FIT_POLICY_MISMATCH', 409);
  } else if (fields.some(field => Object.hasOwn(facts, field) || Object.hasOwn(manifest, field))) fail('ADMIN_FIT_POLICY_MISMATCH', 409);
  const prioritised = facts.products.some(product => product && OPPORTUNITY_FIELDS.some(field => Object.hasOwn(product, field)));
  if (prioritised) {
    if (!current || !OPPORTUNITY_POLICIES.includes(facts.opportunity_policy_id) || manifest.opportunity_policy_id !== facts.opportunity_policy_id ||
        facts.products.some(product => !product || product.opportunity_policy_id !== facts.opportunity_policy_id ||
          !OPPORTUNITY_FIELDS.every(field => Object.hasOwn(product, field)))) fail('ADMIN_OPPORTUNITY_POLICY_MISMATCH', 409);
  } else if (Object.hasOwn(facts, 'opportunity_policy_id') || Object.hasOwn(manifest, 'opportunity_policy_id')) fail('ADMIN_OPPORTUNITY_POLICY_MISMATCH', 409);
  for (const product of facts.products) {
    if (!product || typeof product !== 'object') fail('ADMIN_FACT_REQUIREMENTS_NOT_MET', 409);
    const states = product.kpi_states;
    if (!Array.isArray(states) || states.length !== (current ? 10 : 13) || states.some(state => !FIT_STATES.includes(state))) fail('ADMIN_FACT_REQUIREMENTS_NOT_MET', 409);
    const matches = states.filter(state => state === 'Meets').length;
    if (matches < (current ? 5 : 7) || product.kpi_matches !== matches) fail('ADMIN_FACT_REQUIREMENTS_NOT_MET', 409);
    if (prioritised) {
      const basis = product.opportunity_basis;
      if (!basis || typeof basis !== 'object' || Array.isArray(basis)) fail('ADMIN_OPPORTUNITY_BASIS_INVALID', 409);
      let available, positive;
      if (product.opportunity_policy_id === OPPORTUNITY_PLATFORM_POLICY) {
        const fields = ['available_sources', 'evidence_by_source', 'positive_source_ids', 'positive_sources', 'source_ids'];
        const sortedSources = values => Array.isArray(values) && values.every(value => OPPORTUNITY_SOURCES.includes(value)) &&
          same(values, [...new Set(values)].sort());
        if (!same(Object.keys(basis).sort(), fields) || !integer(basis.available_sources, 1, OPPORTUNITY_SOURCES.length) ||
            !integer(basis.positive_sources, 0, basis.available_sources) || !sortedSources(basis.source_ids) ||
            !sortedSources(basis.positive_source_ids) || basis.source_ids.length !== basis.available_sources ||
            basis.positive_source_ids.length !== basis.positive_sources ||
            basis.positive_source_ids.some(source => !basis.source_ids.includes(source)) ||
            !basis.evidence_by_source || typeof basis.evidence_by_source !== 'object' || Array.isArray(basis.evidence_by_source) ||
            !same(Object.keys(basis.evidence_by_source).sort(), basis.source_ids)) fail('ADMIN_OPPORTUNITY_BASIS_INVALID', 409);
        if (!Array.isArray(product.source_refs)) fail('ADMIN_OPPORTUNITY_SOURCE_REFERENCE_INVALID', 409);
        const seen = new Set(), references = new Set(product.source_refs.map(ref => ref?.id));
        for (const [source, refs] of Object.entries(basis.evidence_by_source)) {
          if (!Array.isArray(refs) || !refs.length || refs.some(ref => typeof ref !== 'string' || !ID.test(ref)) ||
              !same(refs, [...new Set(refs)].sort()) || refs.some(ref => seen.has(ref) || !references.has(ref))) fail('ADMIN_OPPORTUNITY_SOURCE_REFERENCE_INVALID', 409);
          for (const ref of refs) seen.add(ref);
        }
        available = basis.available_sources; positive = basis.positive_sources;
      } else {
        if (!same(Object.keys(basis).sort(), ['available_families', 'positive_families']) ||
            !integer(basis.available_families, 1, 3) || !integer(basis.positive_families, 0, basis.available_families)) fail('ADMIN_OPPORTUNITY_BASIS_INVALID', 409);
        available = basis.available_families; positive = basis.positive_families;
      }
      const expected = available >= 2 ? (matches >= 6 ? 1 : 2) : positive >= 1 ? (matches >= 6 ? 3 : 4) : null;
      if (!integer(product.opportunity_priority, 1, 4) || product.opportunity_priority !== expected) fail('ADMIN_OPPORTUNITY_PRIORITY_INVALID', 409);
    }
    if (current) {
      if (!Array.isArray(product.source_refs) || product.source_refs.some(ref => !ref || !ID.test(ref.id ?? '')) ||
          new Set(product.source_refs.map(ref => ref.id)).size !== product.source_refs.length) fail('ADMIN_FACT_REQUIREMENTS_NOT_MET', 409);
      const references = new Set(product.source_refs.map(ref => ref.id));
      const brand = product.brand_binding;
      if (!product.description || typeof product.description !== 'object' || Array.isArray(product.description) ||
          !brand || !['generic_or_small', 'unclear'].includes(brand.verdict) || !['original', 'overlay', 'operator', 'origin'].includes(brand.source) ||
          !HASH.test(brand.description_hash ?? '') || ['overlay', 'origin'].includes(brand.source) &&
            !HASH.test(product.source_revisions?.brand_reviews?.[brand.review_id] ?? '') || !supplierEffortValid(product)) fail('ADMIN_FACT_REQUIREMENTS_NOT_MET', 409);
      if (product.fit_policy_id !== 'fit-10-v1' || !same(product.criterion_ids, FIT_IDS) || product.kpi_total !== 10 ||
          product.fit_tier !== (matches >= 6 ? 'good' : 'decent') || product.supplier_effort_satisfied !== true ||
          !Array.isArray(product.criteria) || !same(product.criteria.map(row => row?.id), FIT_IDS) ||
          product.criteria.some((row, i) => row.state !== states[i] || !['observed', 'inferred', 'not_stated', 'conflicting'].includes(row.basis) ||
            typeof row.reason !== 'string' || !row.reason.trim() || Array.from(row.reason).length > 2000 || !Array.isArray(row.references) ||
            new Set(row.references).size !== row.references.length || row.references.some(ref => typeof ref !== 'string' || !(functional ?
              product.functional_fit_proof?.scoped_facts && Object.hasOwn(product.functional_fit_proof.scoped_facts, ref) : references.has(ref))) ||
            ['Meets', 'Does not meet'].includes(row.state) && (!['observed', 'inferred'].includes(row.basis) ||
              !row.references.length && !(functional ? row.description_inference?.kind === 'functional-type-inference-v1' : descriptionInferenceValid(product, row))) ||
            !functional && row.description_inference != null && !descriptionInferenceValid(product, row))) fail('ADMIN_FACT_REQUIREMENTS_NOT_MET', 409);
      if (Object.hasOwn(product, 'fit_description_contract') && (product.fit_description_contract !== (functional ? 'functional-type-inference-v1' : FIT_DESCRIPTION_CONTRACT) ||
          !product.criteria.every(row => Object.hasOwn(row, 'description_inference')))) fail('ADMIN_FIT_DESCRIPTION_CONTRACT_INVALID', 409);
    } else if (states[2] !== 'Not assessed' || fields.some(field => Object.hasOwn(product, field)) ||
               Object.hasOwn(product, 'fit_tier') || Object.hasOwn(product, 'fit_description_contract') ||
               Array.isArray(product.criteria) && product.criteria.some(row => row?.description_inference != null)) fail('ADMIN_FACT_REQUIREMENTS_NOT_MET', 409);
    if (Object.hasOwn(product, 'supplier_terms_display_version') &&
        (!current || product.supplier_terms_display_version !== SUPPLIER_TERMS_DISPLAY_VERSION ||
         product.opportunity_policy_id !== OPPORTUNITY_PLATFORM_POLICY ||
         product.source_route_contract !== (functional ? 'saved-source-routes-v2' : SOURCE_ROUTE_CONTRACT))) fail('ADMIN_SUPPLIER_TERMS_DISPLAY_VERSION_INVALID', 409);
    validateSourceRouteFacts(product, current);
  }
  if (current) {
    if (!Array.isArray(manifest.languages) || !facts.narratives || typeof facts.narratives !== 'object') fail('ADMIN_CHECKLIST_PHRASES_INVALID', 409);
    for (const language of manifest.languages) {
      const prose = facts.narratives[language];
      if (!prose || !Array.isArray(prose.products) || !same(prose.products.map(row => row?.concept_id), facts.products.map(row => row.concept_id))) fail('ADMIN_CHECKLIST_PHRASES_INVALID', 409);
      for (const row of prose.products) {
        if (!Array.isArray(row.checklist_phrases) || !same(row.checklist_phrases.map(phrase => phrase?.id), FIT_IDS) ||
            row.checklist_phrases.some(phrase => !phrase || !same(Object.keys(phrase).sort(), ['id', 'text']) ||
              typeof phrase.text !== 'string' || !phrase.text.trim() || phrase.text.length > 220 ||
              /<[^>]*>|https?:\/\/|^\s*[=+@]|```/.test(phrase.text) ||
              /\d|[{}]/.test(phrase.text.replace(/\{\{(?:title|rank|kpi_matches|date|retail_price|supplier_price)\}\}/g, '')) ||
              /\b(profit\w*|margin\w*|rto|guarantee\w*\s+(?:winner|sales|returns)|will sell|zero returns|low returns)\b/i.test(phrase.text) ||
              language === 'hi-Latn' && /[\u0900-\u097f]/.test(phrase.text))) fail('ADMIN_CHECKLIST_PHRASES_INVALID', 409);
      }
    }
  }
  return facts;
}

// Async cryptographic verification is explicit and exclusive to the new tuple.
// Legacy callers of validateFitPacket keep their synchronous historical rules.
export async function validateFunctionalPacket(facts, manifest, { asOf = null } = {}) {
  validateFitPacket(facts, manifest);
  if (facts.schema_version !== 3) fail('ADMIN_FUNCTIONAL_TUPLE_INVALID', 409);
  return verifyFunctionalPacket(facts, manifest, { canonical, fail, asOf });
}

function utc(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,3})?(?:Z|\+00:00)$/.test(value)) fail('ADMIN_UTC_REQUIRED');
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed) || new Date(parsed).toISOString().slice(0, 19) !== value.slice(0, 19)) fail('ADMIN_UTC_REQUIRED');
  return parsed;
}
function available(entry, observed) {
  if (!entry.approved || !entry.complete) return null;
  const beginning = Math.max(utc(entry.scheduled_at), utc(entry.server_activated_at));
  const ending = entry.server_deactivated_at ? utc(entry.server_deactivated_at) : null;
  return beginning <= observed && (ending === null || ending > beginning) ? beginning : null;
}

export function validateReleaseIndex(index, client, serverAt) {
  if (!index || index.schema_version !== 1 || index.client_id !== client || !ID.test(client) || !Array.isArray(index.issues) || index.issues.length > 500 || jsonBytes(index).length > INDEX_LIMIT) fail('ADMIN_INDEX_INVALID', 503);
  const seen = new Set(), active = new Set(), observed = utc(serverAt);
  for (const entry of index.issues) {
    if (!entry || !ID.test(entry.issue_id ?? '') || !integer(entry.revision, 1) || !['active', 'withdrawn'].includes(entry.remote_state) ||
        typeof entry.approved !== 'boolean' || typeof entry.complete !== 'boolean' || !HASH.test(entry.package_hash ?? '') || !HASH.test(entry.selection_hash ?? '') ||
        !integer(entry.product_count, 0, 20) || !integer(entry.new_count, 0, entry.product_count) || typeof entry.title !== 'string' || !entry.title.trim() || entry.title.length > 160 ||
        /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(entry.title) || !(entry.publication_ordinal === null || integer(entry.publication_ordinal, 1))) fail('ADMIN_INDEX_INVALID', 503);
    const key = `${entry.issue_id}:${entry.revision}`;
    if (seen.has(key)) fail('ADMIN_INDEX_REVISION_CONFLICT', 503);
    seen.add(key);
    const activated = utc(entry.server_activated_at);
    utc(entry.scheduled_at);
    if (activated > observed) fail('ADMIN_SERVER_TIME_INVALID', 503);
    if (entry.remote_state === 'active') {
      if (active.has(entry.issue_id) || entry.server_deactivated_at != null) fail('ADMIN_INDEX_REVISION_CONFLICT', 503);
      active.add(entry.issue_id);
    } else if (utc(entry.server_deactivated_at) < activated || utc(entry.server_deactivated_at) > observed) fail('ADMIN_SERVER_TIME_INVALID', 503);
    if (!Array.isArray(entry.languages) || entry.languages.length > 2 || new Set(entry.languages).size !== entry.languages.length || entry.languages.some(v => !['en', 'hi-Latn'].includes(v)) || !Array.isArray(entry.files) || entry.files.length > 3) fail('ADMIN_INDEX_FILES_INVALID', 503);
    const expected = new Set(['shortlist.xlsx', ...entry.languages.map(v => `report-${v}.pdf`)]), names = new Set();
    for (const file of entry.files) {
      if (!file || !expected.has(file.name) || names.has(file.name) || file.key !== prefix(client, entry.issue_id, entry.revision) + file.name ||
          file.media_type !== TYPES[file.name] || !HASH.test(file.sha256 ?? '') || !integer(file.size, 1, FILE_LIMIT)) fail('ADMIN_INDEX_FILES_INVALID', 503);
      names.add(file.name);
    }
    if (entry.complete && (entry.publication_ordinal === null || names.size !== expected.size || entry.languages.length !== (entry.publication_ordinal <= 2 ? 2 : 1) || entry.product_count < 10 || entry.new_count * 5 < entry.product_count * 4)) fail('ADMIN_INCOMPLETE_PACKAGE', 503);
  }
  return index;
}

export function projectOrdinal(index, issue, scheduled, serverAt) {
  validateReleaseIndex(index, index.client_id, serverAt);
  const published = new Map(), observed = utc(serverAt);
  for (const entry of index.issues) if (available(entry, observed) !== null) {
    if (published.has(entry.issue_id) && published.get(entry.issue_id) !== entry.publication_ordinal) fail('ADMIN_PUBLISHED_ORDINAL_CONFLICT', 409);
    published.set(entry.issue_id, entry.publication_ordinal);
  }
  if (new Set(published.values()).size !== published.size) fail('ADMIN_PUBLISHED_ORDINAL_CONFLICT', 409);
  if (published.has(issue)) return published.get(issue);
  const queued = new Map(index.issues.filter(e => e.remote_state === 'active' && e.approved && e.complete && !published.has(e.issue_id)).map(e => [e.issue_id, e.scheduled_at]));
  queued.set(issue, scheduled);
  const ordered = [...queued.keys()].sort((a, b) => utc(queued.get(a)) - utc(queued.get(b)) || (a < b ? -1 : a > b ? 1 : 0));
  return published.size + ordered.indexOf(issue) + 1;
}

function approval(value, expected) {
  if (!value || value.approved !== true || !ID.test(value.owner_id ?? '') || Object.entries(expected).some(([key, wanted]) => !same(value[key], wanted))) fail('ADMIN_OWNER_APPROVAL_REQUIRED', 403);
}
function withdraw(entry, at, reason, actor) { Object.assign(entry, { remote_state: 'withdrawn', server_deactivated_at: at, withdrawal_reason: reason,
  ...(actor ? { server_deactivated_by: actor } : {}) }); }

// Only called with a stored, verified manifest. It cannot accept arbitrary indexes.
export async function applyReleaseMutation(index, mutation, serverAt, settings, projection = null) {
  validateReleaseIndex(index, settings.client, serverAt);
  if (!mutation || mutation.client_id !== settings.client || !['activate', 'withdraw'].includes(mutation.kind)) fail('ADMIN_MUTATION_INVALID');
  const result = structuredClone(index);
  if (mutation.kind === 'activate') {
    const m = mutation.manifest;
    if (mutation.allow_synthetic !== false || m?.evidence_type !== 'captured' || !m?.complete || m.approved !== false || m.release_ready !== false ||
        m.client_id !== settings.client || !ID.test(m.issue_id ?? '') || !integer(m.revision, 1)) fail('ADMIN_DELIVERY_INVALID');
    const unsigned = { ...m }; delete unsigned.package_hash;
    if (await hash(encoder.encode(canonical(unsigned))) !== m.package_hash) fail('ADMIN_PACKAGE_HASH_INVALID');
    const indian = new Date(utc(mutation.scheduled_at) + 330 * 60 * 1000);
    if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(settings.time ?? '') || indian.toISOString().slice(0, 10) !== m.release_date || indian.toISOString().slice(11, 16) !== settings.time || indian.getUTCSeconds() !== 0 || indian.getUTCMilliseconds() !== 0 || ![2, 4, 6].includes(indian.getUTCDay())) fail('ADMIN_TIME_CONFIGURATION_REQUIRED');
    const previous = result.issues.filter(e => e.issue_id === m.issue_id), active = previous.find(e => e.remote_state === 'active');
    const replay = active?.revision === m.revision && active.package_hash === m.package_hash;
    if (!replay) {
      if ((active && !same(mutation.expected_previous, { revision: active.revision, package_hash: active.package_hash })) || (!active && mutation.expected_previous !== null)) fail('ADMIN_PREVIOUS_REVISION_MISMATCH', 409);
      if (previous.length && m.revision <= Math.max(...previous.map(e => e.revision))) fail('ADMIN_REVISION_NOT_NEW', 409);
      if (active && available(active, utc(serverAt)) !== null && utc(mutation.scheduled_at) > utc(serverAt) && mutation.approval?.allow_visibility_gap !== true) fail('ADMIN_REPLACEMENT_VISIBILITY_REVIEW_REQUIRED', 409);
    }
    if ((projection?.ordinal ?? projectOrdinal(index, m.issue_id, mutation.scheduled_at, serverAt)) !== m.publication_ordinal) fail('ADMIN_QUEUE_REPROJECTION_REQUIRED', 409);
    if (m.publication_ordinal > 2 && (!['en', 'hi-Latn'].includes(settings.language) || mutation.selected_language !== settings.language || !same(m.languages, [settings.language]))) fail('ADMIN_LANGUAGE_CHOICE_REQUIRED');
    approval(mutation.approval, { client_id: settings.client, issue_id: m.issue_id, revision: m.revision, package_hash: m.package_hash,
      selection_hash: m.selection_hash, publication_ordinal: m.publication_ordinal, scheduled_at: mutation.scheduled_at });
    if (!replay && utc(mutation.scheduled_at) <= utc(serverAt) && mutation.approval.allow_late !== true) fail('ADMIN_LATE_CONSEQUENCE_REVIEW_REQUIRED', 409);
    if (replay) return result;
    if (active) withdraw(active, serverAt, 'superseded', settings.actor);
    const path = prefix(settings.client, m.issue_id, m.revision);
    result.issues.push({ issue_id: m.issue_id, revision: m.revision, package_hash: m.package_hash, selection_hash: m.selection_hash,
      publication_ordinal: m.publication_ordinal, product_count: m.product_count, new_count: m.new_count, languages: structuredClone(m.languages), files: structuredClone(m.files),
      title: m.languages[0] === 'en' ? 'Product picks' : 'Product sujhaav', summary: '', remote_state: 'active', approved: true, complete: true,
      scheduled_at: mutation.scheduled_at, server_activated_at: serverAt, owner_id: mutation.approval.owner_id,
      ...(settings.actor ? { server_actor_email: settings.actor } : {}), evidence_type: m.evidence_type,
      facts_hash: m.facts_hash, concept_ids: structuredClone(m.concept_ids), manifest_key: path + 'delivery-manifest.json',
      manifest_sha256: await hash(jsonBytes(m)), facts_key: path + 'final_report_data.json', facts_sha256: m.artifacts.facts.sha256 });
  } else {
    if (!ID.test(mutation.issue_id ?? '') || !integer(mutation.revision, 1) || !HASH.test(mutation.package_hash ?? '')) fail('ADMIN_MUTATION_INVALID');
    approval(mutation.approval, { client_id: settings.client, issue_id: mutation.issue_id, revision: mutation.revision, package_hash: mutation.package_hash, action: 'withdraw' });
    const entry = result.issues.find(e => e.issue_id === mutation.issue_id && e.revision === mutation.revision);
    if (!entry || entry.package_hash !== mutation.package_hash) fail('ADMIN_PREVIOUS_REVISION_MISMATCH', 409);
    if (entry.remote_state !== 'withdrawn') withdraw(entry, serverAt, 'owner_withdrawal', settings.actor);
  }
  for (let pass = 0; projection === null && pass <= result.issues.length; pass++) {
    const queued = result.issues.filter(e => e.remote_state === 'active' && e.approved && e.complete && available(e, utc(serverAt)) === null);
    const bad = queued.filter(e => projectOrdinal(result, e.issue_id, e.scheduled_at, serverAt) !== e.publication_ordinal);
    if (!bad.length) break;
    for (const entry of bad) withdraw(entry, serverAt, 'queue_reprojection_required', settings.actor);
  }
  return validateReleaseIndex(result, settings.client, serverAt);
}

function settings(env) {
  const emails = (value, allowEmpty = false) => {
    const parsed = JSON.parse(value);
    if (!Array.isArray(parsed) || (!allowEmpty && !parsed.length) || parsed.length > 20 || parsed.some(v => typeof v !== 'string' || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v))) fail('ADMIN_NOT_CONFIGURED', 503);
    return new Set(parsed.map(v => v.toLowerCase()));
  };
  if (!ID.test(env.CLIENT_ID ?? '') || typeof env.ACCESS_AUD !== 'string' || !env.ACCESS_AUD || !env.REPORTS || typeof env.REPORTS.get !== 'function' || typeof env.REPORTS.put !== 'function') fail('ADMIN_NOT_CONFIGURED', 503);
  const operators = emails(env.OPERATOR_EMAILS), clients = emails(env.ALLOWED_EMAILS, true);
  if ([...operators].some(email => clients.has(email))) fail('ADMIN_NOT_CONFIGURED', 503);
  return { client: env.CLIENT_ID, aud: env.ACCESS_AUD, operators, time: env.PUBLICATION_TIME, language: env.ONGOING_LANGUAGE };
}
function reply(body, status = 200, type = 'application/json', headers = {}) {
  return new Response(body, { status, headers: { 'Content-Type': type, 'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff', ...headers } });
}
async function body(request, limit) {
  const length = request.headers.get('Content-Length');
  if (length !== null && (!/^\d+$/.test(length) || Number(length) > limit)) fail('ADMIN_BODY_LIMIT', 413);
  if (!request.body) fail('ADMIN_BODY_INVALID');
  const reader = request.body.getReader(), chunks = []; let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read(); if (done) break;
      size += value.byteLength;
      if (size > limit) { await reader.cancel(); fail('ADMIN_BODY_LIMIT', 413); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  if (!size) fail('ADMIN_BODY_INVALID');
  const bytes = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return bytes;
}
function parse(bytes) {
  let text, value;
  try { text = decoder.decode(bytes); value = JSON.parse(text); }
  catch { fail('ADMIN_JSON_INVALID'); }
  // JSON.parse accepts duplicate keys. Reject those without writing a second
  // JSON parser: grammar is already checked; scan only nesting/property names.
  const stack = [];
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === '{' || ch === '[') {
      stack.push({ object: ch === '{', expectingKey: ch === '{', keys: new Set() });
      if (stack.length > 80) fail('ADMIN_JSON_DEPTH_LIMIT');
    } else if (ch === '}' || ch === ']') stack.pop();
    else if (ch === ',') { if (stack.at(-1)?.object) stack.at(-1).expectingKey = true; }
    else if (ch === '"') {
      const start = i;
      for (i++; i < text.length; i++) { if (text[i] === '\\') i++; else if (text[i] === '"') break; }
      const current = stack.at(-1);
      if (current?.object && current.expectingKey) {
        const key = JSON.parse(text.slice(start, i + 1));
        if (current.keys.has(key)) fail('ADMIN_JSON_DUPLICATE_FIELD');
        current.keys.add(key); current.expectingKey = false;
      }
    }
  }
  return value;
}
async function objectBytes(object, limit) {
  if (!object || !integer(object.size, 1, limit) || typeof object.arrayBuffer !== 'function') fail('ADMIN_OBJECT_UNAVAILABLE', 503);
  const bytes = new Uint8Array(await object.arrayBuffer());
  if (bytes.byteLength !== object.size) fail('ADMIN_OBJECT_CHANGED', 503);
  return bytes;
}
async function readIndex(bucket, client, clock) {
  const stored = await bucket.get(`clients/${client}/release-index.json`);
  const observed = clock().toISOString();
  if (!stored) return { index: { schema_version: 1, client_id: client, issues: [] }, etag: null, server_observed_at: observed, verified: true, authority: 'release_index' };
  if (typeof stored.etag !== 'string' || !stored.etag) fail('ADMIN_ETAG_MISSING', 503);
  const index = parse(await objectBytes(stored, INDEX_LIMIT));
  if (index?.schema_version === 2) validateReleaseRoot(index, client, observed);
  else validateReleaseIndex(index, client, observed);
  return { index, etag: stored.etag, server_observed_at: observed, verified: true, authority: 'release_index' };
}

async function privateDraftManifest(bucket, client, issue, revision) {
  const path = prefix(client, issue, revision);
  const manifest = parse(await objectBytes(await bucket.get(path + 'delivery-manifest.json'), INDEX_LIMIT));
  if (!manifest || ![1, 2].includes(manifest.schema_version) || manifest.client_id !== client || manifest.issue_id !== issue || manifest.revision !== revision ||
      manifest.evidence_type !== 'captured' || manifest.complete !== true || manifest.approved !== false || manifest.release_ready !== false ||
      manifest.state !== 'draft_ready_for_review' || !HASH.test(manifest.package_hash ?? '') || !HASH.test(manifest.facts_hash ?? '') ||
      !integer(manifest.product_count, 10, 20) || !integer(manifest.new_count, 0, manifest.product_count) || manifest.new_count * 5 < manifest.product_count * 4 ||
      !integer(manifest.publication_ordinal, 1) || !Array.isArray(manifest.languages) ||
      manifest.languages.some(value => !['en', 'hi-Latn'].includes(value)) || new Set(manifest.languages).size !== manifest.languages.length ||
      manifest.languages.length !== (manifest.publication_ordinal <= 2 ? 2 : 1)) fail('ADMIN_PRIVATE_DRAFT_INVALID', 409);
  if (manifest.schema_version === 2 && (manifest.recommendation_contract !== 'functional-type-examples-v1' ||
      manifest.workbook_contract !== 'fit-checklist-examples-v2' || !HASH.test(manifest.asset_receipt_hash ?? '')) ||
      manifest.schema_version === 1 && Object.hasOwn(manifest, 'recommendation_contract')) fail('ADMIN_PRIVATE_DRAFT_INVALID', 409);
  const body = { ...manifest }; delete body.package_hash;
  if (await hash(encoder.encode(canonical(body))) !== manifest.package_hash) fail('ADMIN_PRIVATE_DRAFT_CHANGED', 409);
  const expected = ['shortlist.xlsx', ...manifest.languages.map(language => `report-${language}.pdf`)];
  if (!Array.isArray(manifest.files) || manifest.files.length !== expected.length || new Set(manifest.files.map(file => file?.name)).size !== expected.length ||
      manifest.files.some(file => !file || !expected.includes(file.name) || file.key !== path + file.name || file.media_type !== TYPES[file.name] ||
        !HASH.test(file.sha256 ?? '') || !integer(file.size, 1, FILE_LIMIT))) fail('ADMIN_PRIVATE_DRAFT_INVALID', 409);
  const facts = manifest.artifacts?.facts;
  if (!facts || !HASH.test(facts.sha256 ?? '') || !integer(facts.size, 1, FACTS_LIMIT)) fail('ADMIN_PRIVATE_DRAFT_INVALID', 409);
  return manifest;
}

async function privateDraftList(bucket, client, clock) {
  if (typeof bucket.list !== 'function' || typeof bucket.head !== 'function') fail('ADMIN_PRIVATE_DRAFT_LIST_UNAVAILABLE', 503);
  const index = await readIndex(bucket, client, clock);
  const listed = new Set((index.index.issues ?? []).map(issue => `${issue.issue_id}:${issue.revision}`));
  const start = `clients/${client}/issues/`, keys = new Set(), cursors = new Set();
  let cursor, limited = false;
  // A bounded scan handles the actual single-client volume without an index
  // writer or a new database. Never silently call a truncated scan exhaustive.
  for (let page = 0; page < 3; page++) {
    const listing = await bucket.list({ prefix: start, limit: 1000, ...(cursor ? { cursor } : {}) });
    if (!listing || !Array.isArray(listing.objects) || listing.objects.length > 1000 || typeof listing.truncated !== 'boolean') fail('ADMIN_PRIVATE_DRAFT_LIST_UNAVAILABLE', 503);
    for (const object of listing.objects) {
      if (typeof object?.key !== 'string' || !object.key.startsWith(start)) fail('ADMIN_PRIVATE_DRAFT_LIST_UNAVAILABLE', 503);
      if (/\/r[1-9]\d*\/delivery-manifest\.json$/.test(object.key)) keys.add(object.key);
    }
    if (!listing.truncated) break;
    if (typeof listing.cursor !== 'string' || !listing.cursor || cursors.has(listing.cursor)) fail('ADMIN_PRIVATE_DRAFT_LIST_UNAVAILABLE', 503);
    cursor = listing.cursor; cursors.add(cursor); limited = page === 2;
  }
  let candidates = [...keys].map(key => {
    const match = /^([A-Za-z0-9_-]{1,80})\/r([1-9]\d*)\/delivery-manifest\.json$/.exec(key.slice(start.length));
    return match && integer(Number(match[2]), 1) ? { key, issue: match[1], revision: Number(match[2]) } : null;
  }).filter(candidate => candidate && !listed.has(`${candidate.issue}:${candidate.revision}`))
    .sort((a, b) => b.issue.localeCompare(a.issue) || b.revision - a.revision);
  if (index.index.schema_version === 2) {
    const tree = new ReleaseTree(bucket, index.index), validateEntry = v2EntryValidator(client), kept = [];
    for (const candidate of candidates.slice(0, 8)) {
      const issue = await issueProjection(tree, candidate.issue, validateEntry, index.server_observed_at);
      if (!issue || candidate.revision > issue.latest.revision) kept.push(candidate);
    }
    limited ||= candidates.length > 8; candidates = kept;
  }
  const drafts = [], skipped = [];
  // Eight versions plus three listing pages remain below fifty storage reads
  // even when every version has both PDFs and its shared workbook/facts.
  for (const candidate of candidates.slice(0, 8)) {
    try {
      const manifest = await privateDraftManifest(bucket, client, candidate.issue, candidate.revision);
      const objects = [...manifest.files, { key: prefix(client, candidate.issue, candidate.revision) + 'final_report_data.json',
        size: manifest.artifacts.facts.size, sha256: manifest.artifacts.facts.sha256 }];
      for (const file of objects) {
        const object = await bucket.head(file.key);
        // This is an existence/metadata check, not a claim that every report
        // was downloaded. Each click below verifies the full actual bytes.
        if (!object || object.size !== file.size || object.customMetadata?.sha256 !== file.sha256) fail('ADMIN_PRIVATE_DRAFT_FILES_INCOMPLETE', 409);
      }
      drafts.push({ issue_id: manifest.issue_id, revision: manifest.revision, product_count: manifest.product_count,
        package_hash: manifest.package_hash, state: 'stored_for_owner_review', approved: false, client_available: false,
        files: manifest.files.map(file => ({ name: file.name, size: file.size, sha256: file.sha256,
          url: `/admin/download/${manifest.issue_id}/${manifest.revision}/${file.name}` })) });
    } catch (error) {
      skipped.push({ issue_id: candidate.issue, revision: candidate.revision,
        reason: error instanceof Failure ? error.message : 'ADMIN_PRIVATE_DRAFT_CHECK_FAILED' });
    }
  }
  return { schema_version: 1, client_id: client, server_observed_at: clock().toISOString(), drafts, skipped,
    scan_limited: limited || candidates.length > 8, checked_versions: drafts.length + skipped.length,
    index_write_attempted: false, publication_attempted: false };
}

const draftToken = value => {const token=btoa(String.fromCharCode(...encoder.encode(canonical(value)))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');if(token.length>3072)fail('ADMIN_PRIVATE_DRAFT_CURSOR_LIMIT',409);return token;};
async function privateDraftPage(bucket,client,clock,token=null) {
  if(typeof bucket.list!=='function'||typeof bucket.head!=='function')fail('ADMIN_PRIVATE_DRAFT_LIST_UNAVAILABLE',503);
  const index=await readIndex(bucket,client,clock),source_prefix=`clients/${client}/issues/`,index_hash=await releaseDigest(index.index);
  let saved=null;
  if(token!==null){
    if(typeof token!=='string'||!/^[A-Za-z0-9_-]{1,3072}$/.test(token))fail('ADMIN_PRIVATE_DRAFT_CURSOR_INVALID');
    try{saved=parseRelease(Uint8Array.from(atob(token.replace(/-/g,'+').replace(/_/g,'/')),c=>c.charCodeAt(0)));}catch{fail('ADMIN_PRIVATE_DRAFT_CURSOR_INVALID');}
    if(!saved||!same(Object.keys(saved).sort(),['client_id','contract','index_etag','index_hash','offset','source_page_hash','source_prefix','storage_cursor'])||saved.contract!=='private-draft-cursor-v2'||saved.client_id!==client||saved.source_prefix!==source_prefix||
       !(saved.storage_cursor===null||typeof saved.storage_cursor==='string'&&saved.storage_cursor.length>0&&saved.storage_cursor.length<=1024)||!integer(saved.offset,0,100)||!HASH.test(saved.source_page_hash??''))fail('ADMIN_PRIVATE_DRAFT_CURSOR_INVALID');
    if(saved.index_etag!==index.etag||saved.index_hash!==index_hash)fail('ADMIN_PRIVATE_DRAFT_CURSOR_CHANGED',409);
  }
  const listed=new Set((index.index.issues??[]).map(row=>`${row.issue_id}:${row.revision}`)),tree=index.index.schema_version===2?new ReleaseTree(bucket,index.index):null;
  const drafts=[],skipped=[],cursors=new Set(),validateEntry=v2EntryValidator(client);let storageCursor=saved?.storage_cursor??null,offset=saved?.offset??0,checked=0,releasedCount=0,next=null,scanned=0,sourcePages=0,previousLast=null;
  if(storageCursor!==null)cursors.add(storageCursor);
  for(let page=0;page<3;page++){
    const pageCursor=storageCursor,listing=await bucket.list({prefix:source_prefix,limit:100,...(storageCursor===null?{}:{cursor:storageCursor})});sourcePages++;
    if(!listing||!Array.isArray(listing.objects)||listing.objects.length>100||typeof listing.truncated!=='boolean')fail('ADMIN_PRIVATE_DRAFT_LIST_UNAVAILABLE',503);
    const keys=listing.objects.map(row=>row?.key);
    if(keys.some(key=>typeof key!=='string'||!key.startsWith(source_prefix))||new Set(keys).size!==keys.length||previousLast!==null&&keys.length&&keys[0]<=previousLast||
       keys.some((key,i)=>i>0&&key<=keys[i-1]))fail('ADMIN_PRIVATE_DRAFT_LIST_UNAVAILABLE',503);
    if(listing.truncated&&(typeof listing.cursor!=='string'||!listing.cursor||listing.cursor.length>1024||listing.cursor===storageCursor||cursors.has(listing.cursor)))fail('ADMIN_PRIVATE_DRAFT_LIST_UNAVAILABLE',503);
    const source_page_hash=await releaseDigest({keys,truncated:listing.truncated,next_storage_cursor:listing.truncated?listing.cursor:null});
    if(saved&&(source_page_hash!==saved.source_page_hash||offset>keys.length))fail('ADMIN_PRIVATE_DRAFT_CURSOR_CHANGED',409);
    saved=null;
    for(let i=offset;i<keys.length;i++){
      scanned++;const match=/^([A-Za-z0-9_-]{1,80})\/r([1-9]\d*)\/delivery-manifest\.json$/.exec(keys[i].slice(source_prefix.length));
      if(!match||!integer(Number(match[2]),1))continue;
      // After the eighth version, skip only ordinary artifact keys. Keep the
      // first unprocessed manifest in the continuation, without a ninth check.
      // This also avoids a needless empty final page for the last draft's PDFs.
      if(checked===8){next={storage_cursor:storageCursor,source_page_hash,offset:i};break;}
      const issue=match[1],revision=Number(match[2]);checked++;
      try{
        const released=tree?await revisionProjection(tree,issue,revision,validateEntry,index.server_observed_at)!==null:listed.has(`${issue}:${revision}`);
        if(released)releasedCount++;
        else{
          const manifest=await privateDraftManifest(bucket,client,issue,revision),files=[...manifest.files,{key:prefix(client,issue,revision)+'final_report_data.json',size:manifest.artifacts.facts.size,sha256:manifest.artifacts.facts.sha256}];
          for(const file of files){const object=await bucket.head(file.key);if(!object||object.size!==file.size||object.customMetadata?.sha256!==file.sha256)fail('ADMIN_PRIVATE_DRAFT_FILES_INCOMPLETE',409);}
          drafts.push({issue_id:issue,revision,product_count:manifest.product_count,package_hash:manifest.package_hash,state:'stored_for_owner_review',approved:false,client_available:false,
            files:manifest.files.map(file=>({name:file.name,size:file.size,sha256:file.sha256,url:`/admin/download/${issue}/${revision}/${file.name}`}))});
        }
      }catch(error){skipped.push({issue_id:issue,revision,reason:error instanceof Failure||error instanceof ReleasePageError?error.message:'ADMIN_PRIVATE_DRAFT_CHECK_FAILED'});}
    }
    if(next)break;
    if(!listing.truncated)break;
    previousLast=keys.at(-1)??previousLast;cursors.add(listing.cursor);storageCursor=listing.cursor;offset=0;
    if(page===2){
      // Resume this verified page at its end, then follow its actual R2 cursor.
      next={storage_cursor:pageCursor,source_page_hash,offset:keys.length};
    }
  }
  const next_cursor=next===null?null:draftToken({contract:'private-draft-cursor-v2',client_id:client,source_prefix,index_etag:index.etag,index_hash,...next});
  return {schema_version:2,contract:'private-draft-list-v2',client_id:client,source_prefix,index_etag:index.etag,index_hash,server_observed_at:clock().toISOString(),drafts,skipped,
    checked_versions:drafts.length+skipped.length,released_versions:releasedCount,scanned_versions:checked,scanned_objects:scanned,source_pages:sourcePages,scan_limited:next_cursor!==null,next_cursor,index_write_attempted:false,publication_attempted:false};
}

async function verifyStoredPackage(bucket, index, mutation, client, at, history = null) {
  const m = mutation.manifest;
  if (!m || !ID.test(m.issue_id ?? '') || !integer(m.revision, 1) || m.client_id !== client) fail('ADMIN_DELIVERY_INVALID');
  const path = prefix(client, m.issue_id, m.revision);
  const rawManifest = await objectBytes(await bucket.get(path + 'delivery-manifest.json'), INDEX_LIMIT);
  if (!same(parse(rawManifest), m) || await hash(rawManifest) !== await hash(jsonBytes(m))) fail('ADMIN_STORED_MANIFEST_MISMATCH', 409);
  const rawFacts = await objectBytes(await bucket.get(path + 'final_report_data.json'), FACTS_LIMIT);
  const facts = parse(rawFacts);
  if (await hash(rawFacts) !== m.artifacts?.facts?.sha256 || await hash(encoder.encode(canonical(facts))) !== m.facts_hash ||
      facts.issue_id !== m.issue_id || facts.revision !== m.revision || facts.selection_hash !== m.selection_hash || facts.release_date !== m.release_date ||
      facts.evidence_type !== 'captured' || !Array.isArray(facts.products) ||
      facts.products.some(p => !p || typeof p !== 'object') || !same(facts.products.map(p => p.concept_id), m.concept_ids)) fail('ADMIN_STORED_FACTS_MISMATCH', 409);
  const seen = new Set(), published = history?.published ?? new Set(), queued = history?.queued ?? new Set();
  validateFitPacket(facts, m);
  if (facts.schema_version === 3) {
    const effective = new Date(Math.max(utc(at), utc(mutation.scheduled_at))).toISOString();
    await validateFunctionalPacket(facts, m, { asOf: effective });
  }
  for (const entry of history === null ? index.issues : []) {
    if (entry.issue_id === m.issue_id) continue;
    const past = available(entry, utc(at)) !== null, held = entry.remote_state === 'active' && entry.approved && entry.complete;
    if (!past && !held) continue;
    if (!Array.isArray(entry.concept_ids) || entry.concept_ids.length !== entry.product_count || new Set(entry.concept_ids).size !== entry.concept_ids.length || entry.concept_ids.some(id => !ID.test(id))) fail('ADMIN_HISTORY_RECONCILIATION_REQUIRED', 409);
    for (const id of entry.concept_ids) (past ? published : queued).add(id);
  }
  let updates = 0;
  for (const [i, product] of facts.products.entries()) {
    if ([2, 3].includes(facts.schema_version) && await hash(encoder.encode(canonical(product.description))) !== product.brand_binding.description_hash) fail('ADMIN_BRAND_DESCRIPTION_CHANGED', 409);
    if (!ID.test(product.concept_id ?? '') || seen.has(product.concept_id) || queued.has(product.concept_id) || product.rank !== i + 1 ||
        !['Home and Decor', 'Baby Products', 'Home and Kitchen', 'Travel and Outdoor'].includes(product.category) ||
        product.status !== (published.has(product.concept_id) ? 'Updated' : 'New')) fail('ADMIN_FACT_REQUIREMENTS_NOT_MET', 409);
    if (product.status === 'Updated' && (product.material_update_reviewed !== true || typeof product.update_reason !== 'string' || !product.update_reason.trim())) fail('ADMIN_MATERIAL_UPDATE_REVIEW_REQUIRED', 409);
    seen.add(product.concept_id); if (product.status === 'Updated') updates++;
  }
  if (facts.products.length < 10 || facts.products.length > 20 || updates > Math.floor(facts.products.length / 5) || m.product_count !== facts.products.length || m.new_count !== facts.products.length - updates ||
      !same(m.counts, { products: facts.products.length, new: facts.products.length - updates, updates })) fail('ADMIN_FACT_COUNTS_MISMATCH');
  const expected = ['shortlist.xlsx', ...m.languages.map(v => `report-${v}.pdf`)];
  if (!Array.isArray(m.files) || m.files.length !== expected.length || new Set(m.files.map(f => f.name)).size !== expected.length || m.files.some(f => !expected.includes(f.name))) fail('ADMIN_DELIVERY_FILES_INVALID');
  for (const file of m.files) {
    if (file.key !== path + file.name || file.media_type !== TYPES[file.name] || !HASH.test(file.sha256 ?? '') || !integer(file.size, 1, FILE_LIMIT)) fail('ADMIN_DELIVERY_FILES_INVALID');
    const raw = await objectBytes(await bucket.get(file.key), FILE_LIMIT);
    if (raw.length !== file.size || await hash(raw) !== file.sha256) fail('ADMIN_STORED_FILE_CHANGED', 409);
    const artifact = m.artifacts[file.name === 'shortlist.xlsx' ? 'workbook' : 'pdf-' + file.name.slice(7, -4)];
    if (artifact?.sha256 !== file.sha256 || artifact.size !== file.size) fail('ADMIN_DELIVERY_FILES_INVALID');
  }
  const valid_until = facts.schema_version === 3 ? Math.min(...facts.products.map(product => utc(product.support_valid_until))) : null;
  return history === null ? valid_until === null ? null : { valid_until } : { valid_until, facts };
}

const v2EntryValidator = client => (entry, at) => validateReleaseIndex({ schema_version: 1, client_id: client, issues: [entry] }, client, at);
async function v2Queue(tree, at, validateEntry) {
  const result = []; let after = null;
  do {
    const page = await tree.scan('queue', { after, limit: 50 });
    for (const row of page.rows) {
      const q = row.value, entry = await archiveEntry(tree, q.sequence, validateEntry, at), issue = await issueProjection(tree, q.issue_id, validateEntry, at);
      if (!issue || issue.row.active_sequence !== q.sequence || entry.remote_state !== 'active' || await releaseQueueKey(entry) !== row.key ||
          q.available_at !== publicationRef(entry).available_at || q.scheduled_at !== entry.scheduled_at || q.publication_ordinal !== entry.publication_ordinal ||
          q.is_new_issue !== (issue.row.first_exposed_at === null) || firstAvailability(entry, at) !== null) fail('ADMIN_QUEUE_RECONCILIATION_REQUIRED', 409);
      result.push(q);
    }
    after = page.more ? page.last_key : null;
  } while (after);
  return result.sort((a, b) => utc(a.scheduled_at) - utc(b.scheduled_at) || a.issue_id.localeCompare(b.issue_id));
}
async function v2Ordinal(tree, issue, scheduled, at, validateEntry) {
  const current = await issueProjection(tree, issue, validateEntry, at);
  if (current?.row.publication_ordinal !== null && current?.row.publication_ordinal !== undefined) return current.row.publication_ordinal;
  const queued = (await v2Queue(tree, at, validateEntry)).filter(row => row.is_new_issue && row.issue_id !== issue);
  queued.push({ issue_id: issue, scheduled_at: scheduled });
  queued.sort((a, b) => utc(a.scheduled_at) - utc(b.scheduled_at) || a.issue_id.localeCompare(b.issue_id));
  return tree.root.settled_publication_count + queued.findIndex(row => row.issue_id === issue) + 1;
}
async function verifyV2QueueOrdinals(tree, at, validateEntry) {
  const queued = (await v2Queue(tree, at, validateEntry)).filter(row => row.is_new_issue);
  const bad = queued.filter((row, i) => row.publication_ordinal !== tree.root.settled_publication_count + i + 1);
  if (bad.length) {
    const error = new Failure('ADMIN_QUEUE_OWNER_ACTION_REQUIRED', 409);
    error.details = { affected_issue_ids: bad.map(row => row.issue_id), next_available_at: bad.reduce((a, b) => utc(a.available_at) < utc(b.available_at) ? a : b).available_at,
      action: 'Withdraw or replace these scheduled reports with the owner-approved release action.' }; throw error;
  }
}
async function removeV2Active(tree, entry, at, config, reason) {
  if(firstAvailability(entry,at)===null){tree.unexposedWithdrawals??=[];tree.unexposedWithdrawals.push(structuredClone(entry));}
  const changed = { ...entry }; withdraw(changed, at, reason, config.actor);
  await tree.set('archive', releaseSequenceKey(entry.sequence), changed);
  await tree.set('timeline', timelineKey(entry), null);
  await tree.set('queue', await releaseQueueKey(entry), null);
  for (const id of entry.concept_ids) await tree.set('holds', await releaseHoldKey(id, entry.issue_id), null);
  return changed;
}
async function dependentWithdrawals(tree, mutation, at, config, validateEntry, excludedIssue = null) {
  if (mutation.dependent_withdrawals === undefined) return;
  if (!Array.isArray(mutation.dependent_withdrawals) || mutation.dependent_withdrawals.length > 20 || new Set(mutation.dependent_withdrawals.map(row => row?.issue_id)).size !== mutation.dependent_withdrawals.length) fail('ADMIN_DEPENDENT_WITHDRAWALS_INVALID');
  for (const dependent of mutation.dependent_withdrawals) {
    if (dependent?.kind !== 'withdraw' || dependent.client_id !== config.client || dependent.issue_id === excludedIssue || !ID.test(dependent.issue_id ?? '')) fail('ADMIN_DEPENDENT_WITHDRAWALS_INVALID');
    const owned = await issueProjection(tree, dependent.issue_id, validateEntry, at);
    if (!owned?.active || owned.active.revision !== dependent.revision || owned.active.package_hash !== dependent.package_hash || firstAvailability(owned.active, at) !== null) fail('ADMIN_DEPENDENT_WITHDRAWALS_INVALID');
    approval(dependent.approval, { client_id: config.client, issue_id: dependent.issue_id, revision: dependent.revision, package_hash: dependent.package_hash, action: 'withdraw' });
    await removeV2Active(tree, owned.active, at, config, 'owner_withdrawal'); owned.row.active_sequence = null; await tree.set('issues', await releaseDigest(dependent.issue_id), owned.row);
  }
}
async function aliasProjection(tree, checked) {
  const parents = new Map(), prior = new Map(), edges = [];
  const find = id => { let root = id; while (parents.get(root) !== root) root = parents.get(root); while (parents.get(id) !== root) { const next = parents.get(id); parents.set(id, root); id = next; } return root; };
  const union = (a,b) => { a=find(a); b=find(b); if(a!==b)parents.set(a>b?a:b,a>b?b:a); };
  for (const [id,record] of Object.entries(checked.records)) {
    const members=record.row.input.member_ids; edges.push({id,hash:record.hash,members});
    for (const member of members) if(!prior.has(member)) {
      const group=await canonicalType(tree,member);
      if(group.ancestor_ids.length) fail('ADMIN_ALIAS_SPLIT_REVIEW_REQUIRED',409);
      for(const peer of group.member_ids) {
        const actual=await canonicalType(tree,peer);
        if(actual.canonical_id!==group.canonical_id||!same(actual.member_ids,group.member_ids)||!same(actual.record_refs,group.record_refs)||actual.ancestor_ids.length)fail('ADMIN_ALIAS_PROJECTION_CHANGED',409);
        prior.set(peer,actual);if(!parents.has(peer))parents.set(peer,peer);
      }
      for(const peer of group.member_ids)union(member,peer);
    }
    for(const member of members.slice(1))union(members[0],member);
  }
  const components=new Map();for(const id of parents.keys()){const root=find(id);if(!components.has(root))components.set(root,[]);components.get(root).push(id);}
  for(const review of Object.values(checked.reviews)) {
    const r=review.row, members=r.input.member_ids;
    if(r.result.decision!=='different'||!members.every(id=>parents.has(id))||new Set(members.map(find)).size!==1||new Set(members.map(id=>prior.get(id).canonical_id)).size<=1)continue;
    if(members.every(id=>checked.latestDefinitions[id]?.meaning_hash===r.input.definition_bindings[id].meaning_hash))fail('ADMIN_ALIAS_CONTRADICTORY_REVIEW',409);
  }
  const output=[];
  for(const members of components.values()) {
    members.sort();const refs=new Map();
    for(const id of members)for(const ref of prior.get(id).record_refs??[]){if(refs.has(ref.id)&&refs.get(ref.id)!==ref.hash)fail('ADMIN_ALIAS_RECORD_CHANGED',409);refs.set(ref.id,ref.hash);}
    for(const edge of edges)if(edge.members.some(id=>members.includes(id))){if(refs.has(edge.id)&&refs.get(edge.id)!==edge.hash)fail('ADMIN_ALIAS_RECORD_CHANGED',409);refs.set(edge.id,edge.hash);}
    const record_refs=[...refs].sort(([a],[b])=>a.localeCompare(b)).map(([id,hash])=>({id,hash}));
    output.push({canonical_id:members[0],member_ids:members,record_refs,ancestor_ids:[]});
  }
  return output;
}
async function aliasConflicts(tree,groups,at,validateEntry) {
  const affected=new Set();let next=null;
  for(const group of groups) {
    const exposed=new Set(),holds=[];
    for(const member of group.member_ids) {
      const type=await tree.get('types',await releaseDigest(member));
      if(type?.first_exposed_at!==null&&type!==null) {
        const first=await archiveEntry(tree,type.first_publication?.sequence,validateEntry,at);
        if(type.concept_id!==member||!first.concept_ids.includes(member)||!same(publicationRef(first),type.first_publication)||firstAvailability(first,at)!==type.first_exposed_at)fail('ADMIN_ALIAS_HISTORY_CHANGED',409);
        exposed.add(first.issue_id);
      }
      const page=await tree.scan('holds',{prefix:await releaseDigest(member),limit:50});
      if(page.more)fail('ADMIN_ALIAS_HOLD_BUDGET',409);
      for(const item of page.rows) {
        const h=item.value,e=await archiveEntry(tree,h.sequence,validateEntry,at),owner=await issueProjection(tree,h.issue_id,validateEntry,at);
        if(h.concept_id!==member||await releaseHoldKey(member,h.issue_id)!==item.key||owner?.row.active_sequence!==h.sequence||e.remote_state!=='active'||!e.recommendations?.some(r=>r.concept_id===member&&r.status===h.status))fail('ADMIN_ALIAS_HOLD_CHANGED',409);
        if(firstAvailability(e,at)!==null)exposed.add(h.issue_id);
        else holds.push({hold:h,entry:e});
      }
    }
    const issues=new Set(holds.map(r=>r.hold.issue_id));
    for(const {hold,entry} of holds)if(issues.size>1||holds.filter(r=>r.hold.issue_id===hold.issue_id).length>1||(hold.status==='New'&&[...exposed].some(issue=>issue!==hold.issue_id))) {
      affected.add(hold.issue_id);const available=publicationRef(entry).available_at;if(next===null||utc(available)<utc(next))next=available;
    }
  }
  if(affected.size){const error=new Failure('ADMIN_ALIAS_QUEUE_OWNER_ACTION_REQUIRED',409);error.details={affected_issue_ids:[...affected].sort(),next_available_at:next,action:'Withdraw or replace these scheduled reports with the owner-approved release action before joining these product types.'};throw error;}
}
function aliasOriginals(checked,proof,config) {
  const originals=[];
  for(const [id,record] of Object.entries(checked.records)) {
    const cids=new Set(record.row.input.member_ids),definitions=Object.fromEntries(Object.entries(proof.definitions).filter(([rid])=>{
      const d=checked.definitions[rid].value,b=record.row.input.definition_bindings[d.concept_id];
      return cids.has(d.concept_id)&&d.revision<=checked.definitions[b.definition_id].value.revision;
    }));
    originals.push({key:`clients/${config.client}/release-index-v2/alias-records/${id}.json`,bytes:releaseBytes({contract:'functional-alias-portable-record-v1',record_id:id,record:record.wrapper,definitions})});
  }
  return originals;
}
async function applyV2Aliases(tree,mutation,at,config,validateEntry) {
  const checked=await validateAliasProof(mutation.proof,{asOf:at});
  approval(mutation.approval,{client_id:config.client,action:'aliases',proof_hash:checked.proof_hash});
  const groups=await aliasProjection(tree,checked);
  await dependentWithdrawals(tree,mutation,at,config,validateEntry);
  await aliasConflicts(tree,groups,at,validateEntry);await verifyV2QueueOrdinals(tree,at,validateEntry);
  for(const group of groups)for(const member of group.member_ids)await tree.set('aliases',await releaseDigest(member),{member_id:member,...group});
  await lineageConflicts(tree,groups.flatMap(g=>g.member_ids),at,validateEntry);
  return aliasOriginals(checked,mutation.proof,config);
}
function lineageOriginals(checked,proof,config){
  return Object.entries(checked.records).map(([id,record])=>{
    const input=record.row.input,bindings=input.definition_input.definition_bindings,cids=Object.keys(bindings),oids=Object.values(input.grounding_bindings).flatMap(refs=>Object.keys(refs)),sids=oids.map(oid=>checked.observations[oid].value.semantic_id);
    const definitions=Object.fromEntries(Object.entries(proof.definitions).filter(([rid])=>{const d=checked.definitions[rid].value,b=bindings[d.concept_id];return cids.includes(d.concept_id)&&d.revision<=checked.definitions[b.definition_id].value.revision;}));
    return {key:`clients/${config.client}/release-index-v2/lineage-records/${id}.json`,bytes:releaseBytes({contract:'functional-lineage-portable-record-v1',record_id:id,record:record.wrapper,definitions,observations:Object.fromEntries(Object.entries(proof.observations).filter(([oid])=>oids.includes(oid))),descriptions:Object.fromEntries(Object.entries(proof.descriptions).filter(([sid])=>sids.includes(sid)))})};
  });
}
async function lineageConflicts(tree,ids,at,validateEntry){
  const contexts=new Map(),affectedMembers=new Set();
  for(const id of ids){const c=await lineageTypeContext(tree,id);contexts.set(id,c);for(const cid of [...c.member_ids,...c.ancestor_ids,...c.descendant_ids])affectedMembers.add(cid);}
  const holds=[],exposed=new Map();
  for(const cid of affectedMembers){
    if(!contexts.has(cid))contexts.set(cid,await lineageTypeContext(tree,cid));
    const type=await tree.get('types',await releaseDigest(cid));
    if(type&&type.first_exposed_at!==null){const first=await archiveEntry(tree,type.first_publication?.sequence,validateEntry,at);if(type.concept_id!==cid||!first.concept_ids.includes(cid)||!same(publicationRef(first),type.first_publication)||firstAvailability(first,at)!==type.first_exposed_at)fail('ADMIN_LINEAGE_HISTORY_CHANGED',409);exposed.set(cid,first.issue_id);}
    let after=null;
    do{const page=await tree.scan('holds',{prefix:await releaseDigest(cid),after,limit:50});
      for(const item of page.rows){const h=item.value,e=await archiveEntry(tree,h.sequence,validateEntry,at),owner=await issueProjection(tree,h.issue_id,validateEntry,at);
        if(h.concept_id!==cid||await releaseHoldKey(cid,h.issue_id)!==item.key||owner?.row.active_sequence!==h.sequence||e.remote_state!=='active'||!e.recommendations?.some(r=>r.concept_id===cid&&r.status===h.status))fail('ADMIN_LINEAGE_HOLD_CHANGED',409);
        if(firstAvailability(e,at)!==null)exposed.set(cid,h.issue_id);else holds.push({hold:h,entry:e});
      }after=page.more?page.last_key:null;
    }while(after!==null);
  }
  const related=(left,right)=>{const a=contexts.get(left),b=contexts.get(right);return a.member_ids.some(cid=>[...b.member_ids,...b.ancestor_ids].includes(cid))||b.member_ids.some(cid=>a.ancestor_ids.includes(cid));};
  const affected=new Set();let next=null;
  const add=item=>{affected.add(item.hold.issue_id);const at=publicationRef(item.entry).available_at;if(next===null||utc(at)<utc(next))next=at;};
  for(let index=0;index<holds.length;index++){
    const item=holds[index],h=item.hold,c=contexts.get(h.concept_id),history=new Set([...c.member_ids,...c.ancestor_ids]);
    if(h.status==='New'&&[...exposed].some(([cid,issue])=>history.has(cid)&&issue!==h.issue_id))add(item);
    for(const other of holds.slice(index+1))if(related(h.concept_id,other.hold.concept_id)&&(h.issue_id!==other.hold.issue_id||h.concept_id!==other.hold.concept_id)){add(item);add(other);}
  }
  if(affected.size){const error=new Failure('ADMIN_LINEAGE_QUEUE_OWNER_ACTION_REQUIRED',409);error.details={affected_issue_ids:[...affected].sort(),next_available_at:next,action:'Withdraw or replace these scheduled reports with the owner-approved release action before applying this narrowing history.'};throw error;}
}
async function applyV2Lineage(tree,mutation,at,config,validateEntry){
  const checked=await validateLineageProof(mutation.proof,{asOf:at});approval(mutation.approval,{client_id:config.client,action:'lineage',proof_hash:checked.proof_hash});
  const ids=new Set();
  for(const [id,record]of Object.entries(checked.records)){
    const {parent_id,child_id}=record.row.input;ids.add(parent_id);ids.add(child_id);
    const key=await lineageKey(parent_id,child_id),old=await tree.get('lineage',key),reverse=await tree.get('lineage',await lineageKey(parent_id,child_id,1));
    if(old!==null&&(!same(old,reverse)||old.parent_id!==parent_id||old.child_id!==child_id))fail('ADMIN_LINEAGE_PROJECTION_CHANGED',409);
    if(old===null&&reverse!==null)fail('ADMIN_LINEAGE_PROJECTION_CHANGED',409);
    if(old!==null)await validateLineageAdjacency(tree,{key,value:old},parent_id,0);
    const refs=new Map((old?.record_refs??[]).map(ref=>[ref.id,ref.hash]));if(refs.has(id)&&refs.get(id)!==record.hash)fail('ADMIN_LINEAGE_ORIGINAL_CHANGED',409);refs.set(id,record.hash);
    const value={parent_id,child_id,record_refs:[...refs].sort(([a],[b])=>a.localeCompare(b)).map(([id,hash])=>({id,hash}))};
    await tree.set('lineage',key,value);await tree.set('lineage',await lineageKey(parent_id,child_id,1),value);
  }
  await dependentWithdrawals(tree,mutation,at,config,validateEntry);await lineageConflicts(tree,[...ids],at,validateEntry);await verifyV2QueueOrdinals(tree,at,validateEntry);
  return lineageOriginals(checked,mutation.proof,config);
}
async function v2Mutation(bucket, before, mutation, config, clock) {
  if (!mutation || mutation.client_id !== config.client || !['activate', 'withdraw', 'reconcile','aliases','lineage'].includes(mutation.kind)) fail('ADMIN_MUTATION_INVALID');
  const at = clock().toISOString(), validateEntry = v2EntryValidator(config.client), root = structuredClone(before.index);
  root.generation++; root.previous_root_hash = await releaseDigest(before.index); root.updated_at = at;
  const tree = new ReleaseTree(bucket, root, { readLimit: 384, writeLimit: 400 });
  const settlement = await settleReleaseQueue(tree, { at, validateEntry, limit: 20 });
  if (mutation.kind === 'reconcile' || settlement.remaining) return { tree, settlement, mutation_applied: mutation.kind === 'reconcile' };
  if(mutation.kind==='aliases')return {tree,settlement,mutation_applied:true,identity_originals:await applyV2Aliases(tree,mutation,at,config,validateEntry)};
  if(mutation.kind==='lineage')return {tree,settlement,mutation_applied:true,identity_kind:'lineage',identity_originals:await applyV2Lineage(tree,mutation,at,config,validateEntry)};
  if (!['activate', 'withdraw'].includes(mutation.kind) || mutation.client_id !== config.client) fail('ADMIN_MUTATION_INVALID');
  const id = mutation.kind === 'activate' ? mutation.manifest?.issue_id : mutation.issue_id;
  if (!ID.test(id ?? '')) fail('ADMIN_MUTATION_INVALID');
  const previous = await issueProjection(tree, id, validateEntry, at), entries = previous ? [previous.latest] : [];
  if (previous?.active && previous.active.sequence !== previous.latest.sequence) entries.push(previous.active);
  const index = { schema_version: 1, client_id: config.client, issues: entries.map(row => { const value = { ...row }; delete value.sequence; delete value.recommendations; return value; }) };
  let support = null, ordinal = null;
  if (mutation.kind === 'activate') {
    ordinal = await v2Ordinal(tree, id, mutation.scheduled_at, at, validateEntry);
    const history = await selectedTypeHistory(tree, mutation.manifest.concept_ids ?? [], { issueId: id, at, validateEntry });
    support = await verifyStoredPackage(bucket, index, mutation, config.client, at, history);
  } else if (!previous || !entries.some(row => row.revision === mutation.revision)) fail('ADMIN_PREVIOUS_REVISION_MISMATCH', 409);
  const changed = await applyReleaseMutation(index, mutation, at, config, { ordinal });
  if (mutation.kind === 'activate') {
    const nextEntry = changed.issues.find(row => row.remote_state === 'active');
    if (previous?.active?.revision === nextEntry.revision && previous.active.package_hash === nextEntry.package_hash) return { tree, settlement, mutation_applied: true, replay: true };
    if (previous?.active) await removeV2Active(tree, previous.active, at, config, 'superseded');
    const sequence = root.next_sequence++, entry = { ...nextEntry, sequence };
    entry.recommendations = recommendationsFromFacts(entry, support.facts);
    await tree.set('archive', releaseSequenceKey(sequence), entry);
    await tree.set('revisions', await revisionKey(id, entry.revision), { issue_id: id, revision: entry.revision, sequence });
    await tree.set('timeline', timelineKey(entry), { issue_id: id, sequence });
    const issue = previous?.row ?? { issue_id: id, publication_ordinal: null, first_exposed_at: null };
    issue.latest_sequence = sequence; issue.active_sequence = sequence;
    await tree.set('issues', await releaseDigest(id), issue);
    await tree.set('queue', await releaseQueueKey(entry), { issue_id: id, sequence, available_at: publicationRef(entry).available_at,
      scheduled_at: entry.scheduled_at, publication_ordinal: entry.publication_ordinal, is_new_issue: issue.first_exposed_at === null });
    for (const row of entry.recommendations) await tree.set('holds', await releaseHoldKey(row.concept_id, id), { concept_id: row.concept_id, issue_id: id, sequence, status: row.status });
    await settleReleaseQueue(tree, { at, validateEntry, limit: 20 });
  } else {
    const target = entries.find(row => row.revision === mutation.revision);
    if (!target || target.package_hash !== mutation.package_hash) fail('ADMIN_PREVIOUS_REVISION_MISMATCH', 409);
    if (target.remote_state !== 'withdrawn') {
      if (previous.row.active_sequence !== target.sequence) fail('ADMIN_PREVIOUS_REVISION_MISMATCH', 409);
      await removeV2Active(tree, target, at, config, 'owner_withdrawal'); previous.row.active_sequence = null;
      await tree.set('issues', await releaseDigest(id), previous.row);
    }
  }
  await dependentWithdrawals(tree,mutation,at,config,validateEntry,id);
  await verifyV2QueueOrdinals(tree, at, validateEntry);
  if (support?.valid_until !== null && support?.valid_until !== undefined && Math.max(utc(clock().toISOString()), utc(mutation.scheduled_at)) > support.valid_until) fail('ADMIN_FUNCTIONAL_WITNESS_STALE', 409);
  return { tree, settlement, mutation_applied: true, support_valid_until: support?.valid_until ?? null, scheduled_at: mutation.scheduled_at ?? null };
}
async function commitV2(bucket, before, planned, client, clock) {
  const { tree } = planned;
  validateReleaseRoot(tree.root, client, clock().toISOString());
  if (tree.reads + tree.reachablePending().size * 2 + (planned.identity_originals?.length??0)*2 + 20 > 950) fail('ADMIN_RELEASE_PAGE_BUDGET', 409);
  await tree.persist();
  for(const original of planned.identity_originals??[]){
    await bucket.put(original.key,original.bytes,{onlyIf:new Headers({'If-None-Match':'*'}),httpMetadata:{contentType:'application/json'}});
    const saved=await objectBytes(await bucket.get(original.key),INDEX_LIMIT);
    if(saved.length!==original.bytes.length||await hash(saved)!==await hash(original.bytes))fail(planned.identity_kind==='lineage'?'ADMIN_LINEAGE_ORIGINAL_CHANGED':'ADMIN_ALIAS_ORIGINAL_CHANGED',409);
  }
  const finalAt=clock().toISOString(),crossed=(tree.unexposedWithdrawals??[]).filter(entry=>firstAvailability(entry,finalAt)!==null);
  if(crossed.length){const error=new Failure('ADMIN_RELEASE_AVAILABILITY_CHANGED',409);error.details={affected_issue_ids:[...new Set(crossed.map(e=>e.issue_id))].sort(),action:'Reconcile current report availability, then retry the owner-approved change.'};throw error;}
  if (planned.support_valid_until !== null && planned.support_valid_until !== undefined &&
      Math.max(utc(clock().toISOString()), utc(planned.scheduled_at)) > planned.support_valid_until) fail('ADMIN_FUNCTIONAL_WITNESS_STALE', 409);
  const onlyIf = before.etag === null ? new Headers({ 'If-None-Match': '*' }) : { etagMatches: before.etag };
  const saved = await bucket.put(`clients/${client}/release-index.json`, releaseBytes(tree.root), { onlyIf, httpMetadata: { contentType: 'application/json' } });
  if (!saved) fail('ADMIN_INDEX_CONFLICT', 409);
  const after = await readIndex(bucket, client, clock);
  if (after.etag !== saved.etag || !same(after.index, tree.root)) fail('ADMIN_WRITE_RECONCILIATION_REQUIRED', 503);
  return { ...after, settlement: planned.settlement, mutation_applied: planned.mutation_applied };
}
async function recoverV2Replay(bucket, before, mutation, config, clock) {
  if (!mutation || mutation.client_id !== config.client || !['activate', 'withdraw','aliases','lineage'].includes(mutation.kind)) return null;
  if(['aliases','lineage'].includes(mutation.kind)){
    const at=clock().toISOString(),lineage=mutation.kind==='lineage',checked=await (lineage?validateLineageProof:validateAliasProof)(mutation.proof,{asOf:at}),tree=new ReleaseTree(bucket,before.index),validateEntry=v2EntryValidator(config.client);
    approval(mutation.approval,{client_id:config.client,action:mutation.kind,proof_hash:checked.proof_hash});
    for(const [id,record] of Object.entries(checked.records)){
      if(lineage){const {parent_id,child_id}=record.row.input,a=await tree.get('lineage',await lineageKey(parent_id,child_id)),b=await tree.get('lineage',await lineageKey(parent_id,child_id,1));if(!a||!same(a,b)||!a.record_refs?.some(ref=>ref.id===id&&ref.hash===record.hash))return null;await lineageTypeContext(tree,parent_id);await lineageTypeContext(tree,child_id);}
      else for(const member of record.row.input.member_ids){const group=await canonicalType(tree,member);if(!group.record_refs?.some(ref=>ref.id===id&&ref.hash===record.hash)||!record.row.input.member_ids.every(cid=>group.member_ids.includes(cid)))return null;}
    }
    for(const original of (lineage?lineageOriginals:aliasOriginals)(checked,mutation.proof,config)){
      const saved=await objectBytes(await bucket.get(original.key),INDEX_LIMIT);
      if(saved.length!==original.bytes.length||await hash(saved)!==await hash(original.bytes))fail(lineage?'ADMIN_LINEAGE_ORIGINAL_CHANGED':'ADMIN_ALIAS_ORIGINAL_CHANGED',409);
    }
    if(mutation.dependent_withdrawals!==undefined){
      if(!Array.isArray(mutation.dependent_withdrawals)||mutation.dependent_withdrawals.length>20||new Set(mutation.dependent_withdrawals.map(v=>v?.issue_id)).size!==mutation.dependent_withdrawals.length)fail('ADMIN_DEPENDENT_WITHDRAWALS_INVALID');
      for(const d of mutation.dependent_withdrawals){
        if(d?.kind!=='withdraw'||d.client_id!==config.client||!ID.test(d.issue_id??''))fail('ADMIN_DEPENDENT_WITHDRAWALS_INVALID');
        approval(d.approval,{client_id:config.client,issue_id:d.issue_id,revision:d.revision,package_hash:d.package_hash,action:'withdraw'});
        const e=await revisionProjection(tree,d.issue_id,d.revision,validateEntry,at);
        if(e?.remote_state!=='withdrawn'||e.package_hash!==d.package_hash||firstAvailability(e,at)!==null)return null;
      }
    }
    return {...before,replay:true,mutation_applied:true};
  }
  const id = mutation.kind === 'activate' ? mutation.manifest?.issue_id : mutation.issue_id;
  if (!ID.test(id ?? '')) return null;
  const selected = await issueProjection(new ReleaseTree(bucket, before.index), id, v2EntryValidator(config.client), clock().toISOString());
  if (!selected) return null;
  const entry = mutation.kind === 'activate' ? selected.active : await revisionProjection(new ReleaseTree(bucket, before.index), id, mutation.revision, v2EntryValidator(config.client), clock().toISOString());
  if (mutation.kind === 'withdraw') {
    if (!entry || entry.remote_state !== 'withdrawn' || entry.revision !== mutation.revision || entry.package_hash !== mutation.package_hash) return null;
    approval(mutation.approval, { client_id: config.client, issue_id: id, revision: entry.revision, package_hash: entry.package_hash, action: 'withdraw' });
  } else {
    const m = mutation.manifest;
    if (!entry || entry.revision !== m.revision || entry.package_hash !== m.package_hash || entry.selection_hash !== m.selection_hash ||
        entry.publication_ordinal !== m.publication_ordinal || entry.scheduled_at !== mutation.scheduled_at || mutation.allow_synthetic !== false) return null;
    approval(mutation.approval, { client_id: config.client, issue_id: id, revision: m.revision, package_hash: m.package_hash,
      selection_hash: m.selection_hash, publication_ordinal: m.publication_ordinal, scheduled_at: mutation.scheduled_at });
    if (!Array.isArray(entry.recommendations)) return null;
    await verifyStoredPackage(bucket, { issues: [] }, mutation, config.client, entry.server_activated_at,
      { published: new Set(entry.recommendations.filter(row => row.status === 'Updated').map(row => row.concept_id)), queued: new Set() });
  }
  return { ...before, replay: true, mutation_applied: true, original_server_activated_at: entry.server_activated_at,
    ...(entry.server_deactivated_at == null ? {} : { original_server_deactivated_at: entry.server_deactivated_at }) };
}
export async function planReleaseMigration(index, { client, sourceEtag, asOf, factsByRevision = {} } = {}) {
  validateReleaseIndex(index, client, asOf);
  return buildReleaseHistory(index.issues, { client, asOf, migration: { source_etag: sourceEtag, source_hash: await releaseDigest(index) }, factsByRevision });
}
async function migrateV2(bucket, before, mutation, config, clock) {
  if (mutation.client_id !== config.client || before.index.schema_version !== 1) fail('ADMIN_MIGRATION_SOURCE_REQUIRED', 409);
  const now = clock().toISOString(), sourceHash = await releaseDigest(before.index), stageKey = `clients/${config.client}/release-index-v2/migrations/${sourceHash}.json`;
  const oldStageObject = await bucket.get(stageKey);
  if (oldStageObject && (typeof oldStageObject.etag !== 'string' || !oldStageObject.etag)) fail('ADMIN_ETAG_MISSING', 503);
  let stage = oldStageObject ? parseRelease(await boundedReleaseBytes(oldStageObject, ROOT_LIMIT)) : null;
  const stageFields = ['contract', 'client_id', 'source_etag', 'source_hash', 'as_of', 'root_hash', 'next_object', 'object_count', 'complete'];
  if (stage && (!same(Object.keys(stage).sort(), stageFields.sort()) || stage.contract !== 'release-migration-stage-v2' || stage.client_id !== config.client ||
      stage.source_hash !== sourceHash || stage.source_etag !== before.etag || !integer(stage.next_object, 0) || !integer(stage.object_count, 0) ||
      stage.next_object > stage.object_count || typeof stage.complete !== 'boolean' || stage.complete !== (stage.next_object === stage.object_count) || !HASH.test(stage.root_hash ?? '') || utc(stage.as_of) > utc(now))) fail('ADMIN_MIGRATION_STAGE_INVALID', 409);
  // If a queued report became visible while staging, start a new deterministic
  // projection at actual server time before the switch. Existing content pages
  // remain immutable and reusable; no old availability is backdated.
  if (stage?.complete && before.index.issues.some(row => firstAvailability(row, stage.as_of) === null && firstAvailability(row, now) !== null)) stage = null;
  const asOf = stage?.as_of ?? now, factsByRevision = {};
  for (const entry of before.index.issues) if (entry.remote_state === 'active' && entry.approved && entry.complete && firstAvailability(entry, asOf) === null) {
    if (!HASH.test(entry.facts_hash ?? '') || !HASH.test(entry.facts_sha256 ?? '') || entry.facts_key !== prefix(config.client, entry.issue_id, entry.revision) + 'final_report_data.json') fail('ADMIN_MIGRATION_FUTURE_FACTS_REQUIRED', 409);
    const raw = await objectBytes(await bucket.get(entry.facts_key), FACTS_LIMIT), facts = parse(raw);
    if (await hash(raw) !== entry.facts_sha256 || await hash(encoder.encode(canonical(facts))) !== entry.facts_hash) fail('ADMIN_MIGRATION_FUTURE_FACTS_CHANGED', 409);
    const recommendations = recommendationsFromFacts(entry, facts);
    factsByRevision[entry.issue_id + ':' + entry.revision] = { issue_id: entry.issue_id, revision: entry.revision, selection_hash: entry.selection_hash,
      products: recommendations.map(row => ({ concept_id: row.concept_id, status: row.status,
        ...(row.meaning_hash === null ? {} : { functional_definition: { meaning_hash: row.meaning_hash } }) })) };
  }
  const planned = await planReleaseMigration(before.index, { client: config.client, sourceEtag: before.etag, asOf, factsByRevision });
  const rootHash = await releaseDigest(planned.root), objects = planned.objects.map(row => row.key);
  if (stage && (stage.root_hash !== rootHash || stage.object_count !== objects.length)) fail('ADMIN_MIGRATION_STAGE_CHANGED', 409);
  const offset = stage?.next_object ?? 0, end = Math.min(objects.length, offset + 20);
  planned.tree.bucket = bucket; await planned.tree.persist({ objects: objects.slice(offset, end) });
  const next = { contract: 'release-migration-stage-v2', client_id: config.client, source_etag: before.etag, source_hash: sourceHash,
    as_of: asOf, root_hash: rootHash, next_object: end, object_count: objects.length, complete: end === objects.length };
  const onlyIf = oldStageObject ? { etagMatches: oldStageObject.etag } : new Headers({ 'If-None-Match': '*' });
  const written = await bucket.put(stageKey, releaseBytes(next), { onlyIf, httpMetadata: { contentType: 'application/json' } });
  if (!written) fail('ADMIN_MIGRATION_STAGE_CONFLICT', 409);
  const checked = await bucket.get(stageKey);
  if (!checked || checked.etag !== written.etag || !same(parseRelease(await boundedReleaseBytes(checked, ROOT_LIMIT)), next)) fail('ADMIN_MIGRATION_RECONCILIATION_REQUIRED', 503);
  if (!next.complete || mutation.finish !== true) return { ...before, migration: next, mutation_applied: false,
    next_action: next.complete ? 'Repeat migrate with finish=true and the same source ETag.' : 'Repeat migrate with the same source ETag; the next bounded group will resume.' };
  // Re-read the current authoritative source immediately before its same-key
  // conditional switch. A concurrent v1 withdrawal/release wins unchanged.
  const current = await readIndex(bucket, config.client, clock);
  if (current.etag !== before.etag || !same(current.index, before.index)) fail('ADMIN_INDEX_CONFLICT', 409);
  if (before.index.issues.some(row => firstAvailability(row, asOf) === null && firstAvailability(row, clock().toISOString()) !== null)) return { ...before, migration: next, mutation_applied: false, next_action: 'Repeat migrate to fold newly elapsed availability before switching.' };
  const condition = before.etag === null ? new Headers({ 'If-None-Match': '*' }) : { etagMatches: before.etag };
  const switched = await bucket.put(`clients/${config.client}/release-index.json`, releaseBytes(planned.root), { onlyIf: condition, httpMetadata: { contentType: 'application/json' } });
  if (!switched) fail('ADMIN_INDEX_CONFLICT', 409);
  const after = await readIndex(bucket, config.client, clock);
  if (after.etag !== switched.etag || !same(after.index, planned.root)) fail('ADMIN_WRITE_RECONCILIATION_REQUIRED', 503);
  return { ...after, migration: next, mutation_applied: true };
}

async function privateObjectRequest(request, env, client, route) {
  const match = /^\/admin\/objects\/([A-Za-z0-9_-]{1,80})\/([1-9]\d*)\/(shortlist\.xlsx|report-en\.pdf|report-hi-Latn\.pdf|final_report_data\.json|delivery-manifest\.json)$/.exec(route);
  if (!match || !integer(Number(match[2]), 1)) fail('ADMIN_NOT_FOUND', 404);
  const [, issue, revision, name] = match, key = prefix(client, issue, Number(revision)) + name;
  const limit = name === 'delivery-manifest.json' ? INDEX_LIMIT : name === 'final_report_data.json' ? FACTS_LIMIT : FILE_LIMIT;
  if (request.method === 'GET') {
    const stored = await env.REPORTS.get(key);
    if (stored === null) fail('ADMIN_OBJECT_NOT_FOUND', 404);
    const raw = await objectBytes(stored, limit);
    return reply(raw, 200, TYPES[name], { 'Content-Length': String(raw.length), 'X-Content-SHA256': await hash(raw) });
  }
  if (request.method !== 'PUT') return reply('ADMIN_METHOD_NOT_ALLOWED', 405, 'text/plain', { Allow: 'GET, PUT' });
  if (request.headers.get('Content-Type') !== TYPES[name] || !HASH.test(request.headers.get('X-Content-SHA256') ?? '')) fail('ADMIN_CONTENT_TYPE_INVALID');
  const raw = await body(request, limit), sha = await hash(raw);
  if (sha !== request.headers.get('X-Content-SHA256')) fail('ADMIN_UPLOAD_HASH_MISMATCH', 409);
  if (name.endsWith('.pdf') && decoder.decode(raw.slice(0, 5)) !== '%PDF-' || name.endsWith('.xlsx') && (raw[0] !== 80 || raw[1] !== 75)) fail('ADMIN_UPLOAD_FORMAT_INVALID');
  if (name.endsWith('.json')) parse(raw);
  await env.REPORTS.put(key, raw, { onlyIf: new Headers({ 'If-None-Match': '*' }), httpMetadata: { contentType: TYPES[name] }, customMetadata: { sha256: sha } });
  const checked = await objectBytes(await env.REPORTS.get(key), limit);
  if (checked.length !== raw.length || await hash(checked) !== sha) fail('ADMIN_IMMUTABLE_OBJECT_CONFLICT', 409);
  return reply(jsonBytes({ key, sha256: sha, size: checked.length }));
}

// Separate Access application/service token. Never manufacture an owner context
// or pass machine requests through the release-index mutation branch.
export async function handleDraftAutomationRequest(request, env, ctx, {
  clock = () => new Date(), verifyServiceAssertion = verifyDraftServiceAssertion,
} = {}) {
  try {
    const config = settings(env);
    if (!HASH.test(env.DRAFT_AUTOMATION_AUD ?? '') || !/^[a-fA-F0-9]{32}\.access$/.test(env.DRAFT_AUTOMATION_CLIENT_ID ?? '')) fail('ADMIN_DRAFT_AUTOMATION_NOT_CONFIGURED', 503);
    if (!ctx?.access) fail('ADMIN_DRAFT_AUTOMATION_CONTEXT_REQUIRED', 403);
    if (ctx.access.aud !== env.DRAFT_AUTOMATION_AUD) fail('ADMIN_DRAFT_AUTOMATION_AUDIENCE_MISMATCH', 403);
    let identity, missing = 'ADMIN_DRAFT_AUTOMATION_IDENTITY_UNAVAILABLE';
    if (typeof ctx.access.getIdentity === 'function') {
      try { identity = await ctx.access.getIdentity(); missing = 'ADMIN_DRAFT_AUTOMATION_IDENTITY_MISSING'; }
      catch { /* Only this unavailable lookup may use a verified assertion. */ }
    }
    if (identity === undefined || identity === null) {
      if (!request.headers.get('Cf-Access-Jwt-Assertion')) fail(missing, 403);
      const verified = await verifyServiceAssertion(request, { issuer: env.DRAFT_AUTOMATION_ISSUER,
        audience: env.DRAFT_AUTOMATION_AUD, clientId: env.DRAFT_AUTOMATION_CLIENT_ID });
      if (verified !== true) throw new ServiceAssertionError('INVALID');
    } else {
      if (typeof identity !== 'object' || Array.isArray(identity)) fail('ADMIN_DRAFT_AUTOMATION_IDENTITY_INVALID', 403);
      // Explicit wrong or non-service identities never get the fallback.
      if (!Object.hasOwn(identity, 'service_token_status')) fail('ADMIN_DRAFT_AUTOMATION_SERVICE_STATUS_MISSING', 403);
      if (identity.service_token_status !== true) fail('ADMIN_DRAFT_AUTOMATION_SERVICE_IDENTITY_REQUIRED', 403);
      if (identity.service_token_id !== env.DRAFT_AUTOMATION_CLIENT_ID) fail('ADMIN_DRAFT_AUTOMATION_SERVICE_ID_MISMATCH', 403);
    }
    const url = new URL(request.url), base = '/_automation/drafts';
    if (!url.pathname.startsWith(base + '/') || url.search || request.headers.get('X-NeuraFlow-Release-Action') !== 'v1' ||
        request.headers.get('Origin') && request.headers.get('Origin') !== url.origin ||
        request.headers.get('Sec-Fetch-Site') && !['same-origin', 'none'].includes(request.headers.get('Sec-Fetch-Site'))) fail('ADMIN_ACTION_REQUIRED', 403);
    const route = '/admin' + url.pathname.slice(base.length);
    if (route === '/admin/index') {
      if (request.method !== 'GET') return reply('ADMIN_METHOD_NOT_ALLOWED', 405, 'text/plain', { Allow: 'GET' });
      return reply(jsonBytes(await readIndex(env.REPORTS, config.client, clock)));
    }
    return await privateObjectRequest(request, env, config.client, route);
  } catch (error) {
    const known = error instanceof Failure || error instanceof ServiceAssertionError;
    const headers = error instanceof ServiceAssertionError && error.check
      ? { 'X-NeuraFlow-Draft-Check': error.check } : {};
    return reply(known ? error.message : 'ADMIN_UNAVAILABLE', known ? error.status : 503, 'text/plain', headers);
  }
}

// clock injection is for offline tests only. No request field can select time.
export async function handleAdminRequest(request, env, ctx, { clock = () => new Date() } = {}) {
  try {
    const config = settings(env);
    if (!ctx?.access || ctx.access.aud !== config.aud || typeof ctx.access.getIdentity !== 'function') fail('ADMIN_ACCESS_REQUIRED', 403);
    let identity;
    try { identity = await ctx.access.getIdentity(); } catch { fail('ADMIN_ACCESS_REQUIRED', 403); }
    if (typeof identity?.email !== 'string' || !config.operators.has(identity.email.toLowerCase())) fail('ADMIN_ACCESS_REQUIRED', 403);
    config.actor = identity.email.toLowerCase();
    const url = new URL(request.url);
    if (url.search && !(['/admin/index-page','/admin/draft-page'].includes(url.pathname) && request.method === 'GET') || request.headers.get('X-NeuraFlow-Release-Action') !== 'v1' ||
        request.headers.get('Origin') && request.headers.get('Origin') !== url.origin ||
        request.headers.get('Sec-Fetch-Site') && request.headers.get('Sec-Fetch-Site') !== 'same-origin' && request.headers.get('Sec-Fetch-Site') !== 'none') fail('ADMIN_ACTION_REQUIRED', 403);
    if (url.pathname === '/admin/drafts') {
      if (request.method !== 'GET') return reply('ADMIN_METHOD_NOT_ALLOWED', 405, 'text/plain', { Allow: 'GET' });
      return reply(jsonBytes(await privateDraftList(env.REPORTS, config.client, clock)));
    }
    if(url.pathname==='/admin/draft-page'){
      if(request.method!=='GET')return reply('ADMIN_METHOD_NOT_ALLOWED',405,'text/plain',{Allow:'GET'});
      if([...url.searchParams.keys()].some(key=>key!=='cursor')||url.searchParams.getAll('cursor').length>1)fail('ADMIN_PRIVATE_DRAFT_CURSOR_INVALID');
      return reply(jsonBytes(await privateDraftPage(env.REPORTS,config.client,clock,url.searchParams.get('cursor'))));
    }
    const draftDownload = /^\/admin\/draft-files\/([A-Za-z0-9_-]{1,80})\/([1-9]\d*)\/(shortlist\.xlsx|report-en\.pdf|report-hi-Latn\.pdf)$/.exec(url.pathname);
    if (draftDownload) {
      if (request.method !== 'GET') return reply('ADMIN_METHOD_NOT_ALLOWED', 405, 'text/plain', { Allow: 'GET' });
      if (!integer(Number(draftDownload[2]), 1)) fail('ADMIN_NOT_FOUND', 404);
      const [, issue, revision, name] = draftDownload;
      const manifest = await privateDraftManifest(env.REPORTS, config.client, issue, Number(revision));
      const file = manifest.files.find(row => row.name === name);
      if (!file) fail('ADMIN_NOT_FOUND', 404);
      const raw = await objectBytes(await env.REPORTS.get(file.key), FILE_LIMIT);
      if (raw.byteLength !== file.size || await hash(raw) !== file.sha256) fail('ADMIN_PRIVATE_DRAFT_FILE_CHANGED', 409);
      return reply(raw, 200, file.media_type, { 'Content-Length': String(file.size), 'X-Content-SHA256': file.sha256,
        'Content-Disposition': `attachment; filename="${name}"` });
    }
    if (url.pathname === '/admin/index-page') {
      if ([...url.searchParams.keys()].some(key => !['kind', 'cursor', 'id', 'revision','endpoint','direction'].includes(key)) || [...url.searchParams.keys()].some(key => url.searchParams.getAll(key).length > 1)) fail('ADMIN_PAGE_QUERY_INVALID');
      const before = await readIndex(env.REPORTS, config.client, clock);
      if (before.index.schema_version !== 2) fail('ADMIN_PAGE_V2_REQUIRED', 409);
      const kind = url.searchParams.get('kind'), tree = new ReleaseTree(env.REPORTS, before.index), cursor = url.searchParams.get('cursor'), id = url.searchParams.get('id');
      if (!['archive', 'issues', 'types', 'aliases', 'queue', 'holds', 'timeline', 'revisions','lineage'].includes(kind)) fail('ADMIN_PAGE_QUERY_INVALID');
      const endpoint=url.searchParams.get('endpoint'),direction=url.searchParams.get('direction');
      if((endpoint!==null||direction!==null)&&(kind!=='lineage'||!ID.test(endpoint??'')||!['0','1'].includes(direction)||id!==null))fail('ADMIN_PAGE_QUERY_INVALID');
      const scanPrefix=endpoint===null?'':await releaseDigest(endpoint)+direction;
      if (id !== null) {
        if (kind === 'revisions') {
          const revision = url.searchParams.get('revision');
          if (!ID.test(id) || cursor !== null || !/^[1-9]\d*$/.test(revision ?? '') || !integer(Number(revision), 1)) fail('ADMIN_PAGE_QUERY_INVALID');
          return reply(jsonBytes({ ...before, kind, value: await revisionProjection(tree, id, Number(revision), v2EntryValidator(config.client), before.server_observed_at) }));
        }
        if (!['issues', 'types', 'aliases'].includes(kind) || !ID.test(id) || cursor !== null || url.searchParams.has('revision')) fail('ADMIN_PAGE_QUERY_INVALID');
        return reply(jsonBytes({ ...before, kind, value: await tree.get(kind, await releaseDigest(id)) }));
      }
      if (url.searchParams.has('revision')) fail('ADMIN_PAGE_QUERY_INVALID');
      const position = cursor === null ? null : await readReleaseCursor(cursor, before.index, kind), reverse = ['archive', 'timeline'].includes(kind);
      if (position && (position.reverse !== reverse || position.prefix !== scanPrefix)) fail('ADMIN_PAGE_QUERY_INVALID');
      const page = await tree.scan(kind, { after: position?.last_key ?? null, reverse, prefix:scanPrefix,limit: 20 });
      if (kind === 'archive') for (const row of page.rows) {
        v2EntryValidator(config.client)(row.value, before.server_observed_at);
        if (releaseSequenceKey(row.value.sequence) !== row.key) fail('ADMIN_PAGE_ENTRY_INVALID', 503);
      }
      return reply(jsonBytes({ ...before, kind, rows: page.rows, next_cursor: page.more ? await releaseCursor(before.index, kind, page.last_key, { reverse, prefix: scanPrefix }) : null }));
    }
    const pageObject = /^\/admin\/release-pages\/([a-z]+)\/([a-f0-9]{64})\.json$/.exec(url.pathname);
    if (pageObject) {
      if (request.method !== 'GET' || url.search || !['archive', 'issues', 'types', 'aliases', 'queue', 'holds', 'timeline', 'revisions','lineage'].includes(pageObject[1])) fail('ADMIN_PAGE_QUERY_INVALID');
      const key = `clients/${config.client}/release-index-v2/pages/${pageObject[1]}/${pageObject[2]}.json`;
      const bytes = await boundedReleaseBytes(await env.REPORTS.get(key), PAGE_LIMIT);
      if (await releaseHash(bytes) !== pageObject[2]) fail('ADMIN_PAGE_CHANGED', 409);
      return reply(bytes, 200, 'application/json', { 'X-Content-SHA256': pageObject[2] });
    }
    const aliasObject=/^\/admin\/alias-records\/(functional-alias-[a-f0-9]{40})\.json$/.exec(url.pathname);
    if(aliasObject){
      if(request.method!=='GET'||url.search)fail('ADMIN_PAGE_QUERY_INVALID');
      const id=aliasObject[1],raw=await objectBytes(await env.REPORTS.get(`clients/${config.client}/release-index-v2/alias-records/${id}.json`),INDEX_LIMIT),capsule=parse(raw);
      if(!capsule||!same(Object.keys(capsule).sort(),['contract','definitions','record','record_id'])||capsule.contract!=='functional-alias-portable-record-v1'||capsule.record_id!==id)fail('ADMIN_ALIAS_ORIGINAL_CHANGED',409);
      await validateAliasProof({contract:'functional-alias-portable-v1',records:{[id]:capsule.record},definitions:capsule.definitions,reviews:{}},{asOf:clock().toISOString()});
      return reply(raw,200,'application/json',{'X-Content-SHA256':await hash(raw)});
    }
    const lineageObject=/^\/admin\/lineage-records\/(functional-lineage-[a-f0-9]{40})\.json$/.exec(url.pathname);
    if(lineageObject){
      if(request.method!=='GET'||url.search)fail('ADMIN_PAGE_QUERY_INVALID');
      const id=lineageObject[1],raw=await objectBytes(await env.REPORTS.get(`clients/${config.client}/release-index-v2/lineage-records/${id}.json`),INDEX_LIMIT),capsule=parse(raw);
      if(!capsule||!same(Object.keys(capsule).sort(),['contract','definitions','descriptions','observations','record','record_id'])||capsule.contract!=='functional-lineage-portable-record-v1'||capsule.record_id!==id)fail('ADMIN_LINEAGE_ORIGINAL_CHANGED',409);
      await validateLineageProof({contract:'functional-lineage-portable-v1',records:{[id]:capsule.record},definitions:capsule.definitions,observations:capsule.observations,descriptions:capsule.descriptions},{asOf:clock().toISOString()});
      return reply(raw,200,'application/json',{'X-Content-SHA256':await hash(raw)});
    }
    if (url.pathname === '/admin/index') {
      if (request.method === 'GET') return reply(jsonBytes(await readIndex(env.REPORTS, config.client, clock)));
      if (request.method !== 'POST') return reply('ADMIN_METHOD_NOT_ALLOWED', 405, 'text/plain', { Allow: 'GET, POST' });
      if (request.headers.get('Content-Type')?.split(';')[0] !== 'application/json') fail('ADMIN_CONTENT_TYPE_INVALID');
      const input = parse(await body(request, INDEX_LIMIT));
      if (!input || !Object.hasOwn(input, 'expected_etag') || !(input.expected_etag === null || typeof input.expected_etag === 'string' && input.expected_etag)) fail('ADMIN_ETAG_REQUIRED');
      const before = await readIndex(env.REPORTS, config.client, clock);
      if (input.contract === 'release-index-v2') {
        if (input.mutation?.client_id !== config.client) fail('ADMIN_MUTATION_INVALID');
        if (input.mutation?.kind === 'migrate' && before.index.schema_version === 2 && before.index.migration?.source_etag === input.expected_etag) return reply(jsonBytes({ ...before, mutation_applied: true, replay: true }));
        if (before.index.schema_version === 2) {
          const replay = await recoverV2Replay(env.REPORTS, before, input.mutation, config, clock);
          if (replay) return reply(jsonBytes(replay));
        }
        if (before.etag !== input.expected_etag) fail('ADMIN_INDEX_CONFLICT', 409);
        if (input.mutation?.kind === 'migrate') return reply(jsonBytes(await migrateV2(env.REPORTS, before, input.mutation, config, clock)));
        if (before.index.schema_version !== 2) fail('ADMIN_PAGE_V2_REQUIRED', 409);
        const planned = await v2Mutation(env.REPORTS, before, input.mutation, config, clock);
        return reply(jsonBytes(await commitV2(env.REPORTS, before, planned, config.client, clock)));
      }
      if (before.index.schema_version === 2) fail('ADMIN_LEGACY_WRITER_FENCED', 409);
      if (before.etag !== input.expected_etag) fail('ADMIN_INDEX_CONFLICT', 409);
      const support = input.mutation?.kind === 'activate'
        ? await verifyStoredPackage(env.REPORTS, before.index, input.mutation, config.client, before.server_observed_at) : null;
      const at = clock().toISOString();
      if (support && Math.max(utc(at), utc(input.mutation.scheduled_at)) > support.valid_until) fail('ADMIN_FUNCTIONAL_WITNESS_STALE', 409);
      const next = await applyReleaseMutation(before.index, input.mutation, at, config);
      if (same(next, before.index)) return reply(jsonBytes(await readIndex(env.REPORTS, config.client, clock)));
      const onlyIf = before.etag === null ? new Headers({ 'If-None-Match': '*' }) : { etagMatches: before.etag };
      const stored = await env.REPORTS.put(`clients/${config.client}/release-index.json`, jsonBytes(next), { onlyIf, httpMetadata: { contentType: 'application/json' } });
      if (!stored) fail('ADMIN_INDEX_CONFLICT', 409);
      const after = await readIndex(env.REPORTS, config.client, clock);
      if (after.etag !== stored.etag || !same(after.index, next)) fail('ADMIN_WRITE_RECONCILIATION_REQUIRED', 503);
      return reply(jsonBytes(after));
    }
    return await privateObjectRequest(request, env, config.client, url.pathname);
  } catch (error) {
    if (error instanceof Failure && error.details) return reply(jsonBytes({ error: error.message, ...error.details }), error.status);
    const known = error instanceof Failure || error instanceof ReleasePageError;
    return reply(known ? error.message : 'ADMIN_UNAVAILABLE', known ? error.status : 503, 'text/plain');
  }
}
