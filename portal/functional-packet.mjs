import { scopedSuppliers } from './supplier-scoped.mjs';
import { validateDefinitionAcceptance } from './definition-proof.mjs';
import { validateExampleBinding } from './example-proof.mjs';

// Saved-only schema-3 reader. No network, mutable research store, model or release
// authority is consulted here. Original decimals stay inside hashed JSON text.
const encoder = new TextEncoder();
const HASH = /^[a-f0-9]{64}$/;
const ID = /^[A-Za-z0-9_-]{1,80}$/;
const FIT_IDS = [1, 2, 5, 6, 7, 8, 9, 11, 12, 13];
const MEANING = ['primary_use', 'mechanism', 'use_context', 'compatibility_constraints', 'included_distinctions', 'excluded_distinctions'];
const DEFINITION = ['report_title', ...MEANING];
const FIT_FIELDS = [...MEANING, 'example_features', 'example_audiences'];
const SITES = ['meta', 'amazon', 'flipkart', 'google-trends', 'blinkit', 'zepto', 'instamart'];
const DEFINITION_INTENT = ['kind', 'concept_id', 'definition', 'meaning_hash', 'presentation_hash', 'basis_refs', 'reviewer', 'reviewed_at', 'job_id', 'job_proof', 'example_decisions', 'accepted'];
const PHYSICAL = { weight: 'weight', packed_weight: 'weight', item_weight: 'weight', weight_kg: 'weight', dimensions: 'dimensions', item_dimensions: 'dimensions', packed_dimensions: 'dimensions', material: 'material', size: 'size', age: 'age', intended_age: 'age', audience: 'audience', compatibility: 'compatibility' };
const pick = (value, fields) => Object.fromEntries(fields.map(field => [field, value[field]]));
const omit = (value, fields) => Object.fromEntries(Object.entries(value).filter(([key]) => !fields.includes(key)));
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const integer = (value, min = 0, max = Number.MAX_SAFE_INTEGER) => Number.isSafeInteger(value) && value >= min && value <= max;
const text = value => typeof value === 'string' && value.trim() ? value : null;
const ordered = value => Array.isArray(value) && value.every(v => typeof v === 'string') && JSON.stringify(value) === JSON.stringify([...new Set(value)].sort());
const DAY = 86400000;
const routeDefinitions=[['meta-ads','Facebook Ads Library','meta'],['amazon-bestsellers','Amazon India Bestsellers','marketplace'],['amazon-movers-and-shakers','Amazon India Movers & Shakers','marketplace'],['amazon-search','Amazon India product search','marketplace'],['flipkart-search','Flipkart product search','marketplace'],['google-trends','Google Trends India','trends'],['blinkit','Blinkit','quickcommerce'],['zepto','Zepto','quickcommerce'],['instamart','Instamart','quickcommerce'],['google-suppliers','Google supplier search','supplier'],['google-maps','Google Maps supplier search','supplier'],['indiamart','IndiaMART','supplier'],['tradeindia','TradeIndia','supplier'],['company-pages','Supplier company pages','supplier']];
const routeNames=routeDefinitions.map(r=>r[0]),supplierRoutes=routeDefinitions.filter(r=>r[2]==='supplier').map(r=>r[0]);
// Mirrors Python supplier_effort: retired routes are saved history, not current
// supplier effort; a reparse supersedes only its own original (route + pass), the
// newest parser version winning; "blocked"/"failed" only when every current
// original failed or was refused.
export const retiredSupplierRoutes=['google-suppliers'];
const supplierRouteAliases={'google-public-html':'google-suppliers','google-browser-html':'google-suppliers','known-supplier-pages':'company-pages'};
const supplierOutcomeRank={usable_leads:6,interpretation_pending:6,completed_empty:5,partial:4,blocked:3,failed:2,prelaunch_failed:1};
const supplierParserRank=reference=>{const m=/^capture-parser-([1-9][0-9]{0,3})(?:-[0-9a-f]{16})?\.json$/.exec(reference.replace(/\\/g,'/').split('/').at(-1));return m?Number(m[1]):0;};
export function currentSupplierOutcome(rows,fail){
  const current=new Map(),later=(a,b)=>{for(let i=0;i<a.length;i++){if(a[i]>b[i])return true;if(a[i]<b[i])return false;}return false;};
  for(const [id,r] of rows){const slot=JSON.stringify([r.route,r.pass_id]),order=[supplierParserRank(r.reference),utc(r.at,fail),id],old=current.get(slot);
    if(!old||later(order,old.order))current.set(slot,{order,outcome:r.internal_outcome});}
  let best=null;for(const {outcome} of current.values())if(best===null||supplierOutcomeRank[outcome]>supplierOutcomeRank[best])best=outcome;
  return best===null?'not_attempted':best==='usable_leads'?'interpretation_pending':best;
}
const memoryTables=['captures','observations','descriptions','concepts','associations','assessments','problems','jobs','query_outcomes','query_tasks','batch_plans','source_checks','catalogue_additions','supplier_assessments','trends_evidence','issue_reservations','published_history','market_reviews','batch_runs','brand_reviews'];
const routeProofTables=['concepts','descriptions','associations','query_tasks','source_checks','observations','captures','trends_evidence','jobs','brand_reviews'];
const metricLabels={relevant_ads:['Relevant ads in saved sample','ads'],advertisers:['Identified advertisers','advertisers'],active_ads:['Ads marked active','ads'],unknown_active_status:['Ads without an active-status answer','ads'],missing_advertiser_ids:['Ads without an advertiser identity','ads'],relevant_listings:['Relevant listing examples','listings'],list_rank_bounds:['Observed list positions','list rank, not units sold'],displayed_prices:['Listings with a displayed price','listings'],available_listings:['Listings marked available','listings across the checked locations'],unavailable_listings:['Listings marked unavailable','listings across the checked locations'],unknown_availability:['Listings without an availability answer','listings'],chart_intervals:['Saved chart intervals','intervals'],nonzero_points:['Chart intervals with nonzero interest','intervals'],previous_mean_bounds:['Previous four-period interest','relative interest index, not sales'],recent_mean_bounds:['Recent four-period interest','relative interest index, not sales']};
function measurementText(metric){const [label,unit]=metricLabels[metric.name];if(metric.value===null)return label+': not stated';const value=Array.isArray(metric.value)?metric.value[0]===metric.value[1]?String(metric.value[0]):metric.value.join('–'):String(metric.value);return label+': '+value+' ('+unit+')';}
function outcomeDisplay(outcome,actual){
  if(['not_attempted','not_connected'].includes(outcome))return ['Not tried','No product check is saved for this route.'];
  if(outcome==='not_applicable')return ['Not applicable','This check does not apply to the saved product scope.'];
  if(outcome==='completed_relevant')return ['Relevant result found','Saved results were matched to this product type.'];
  if(outcome==='completed_no_results')return ['No usable result found','The completed check found no usable result for this product type.'];
  if(outcome==='interpretation_pending')return ['Results need review','Source results are saved; their match to this product still needs review.'];
  if(outcome==='insufficient')return ['Result unclear','The check did not provide enough usable information; this is not a product failure.'];
  if(outcome==='in_progress')return actual?['Check in progress','An attempted check still needs its saved result.']:['Not tried','The check is prepared, but no actual request is confirmed.'];
  return !actual?['Not tried','No request for this product was sent; the route could not start or is waiting.']:[outcome==='failed'?'Check failed':'Check blocked','The attempted check did not produce a usable answer; this is not a product failure.'];
}

function utc(value, fail) {
  if (typeof value !== 'string' || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value)) fail('ADMIN_FUNCTIONAL_DATE_INVALID', 409);
  const at = Date.parse(value);
  if (!Number.isFinite(at) || new Date(at).toISOString().slice(0, 19) !== value.slice(0, 19)) fail('ADMIN_FUNCTIONAL_DATE_INVALID', 409);
  return at;
}

// Nodes retain exact original fragments. This is essential: Python's 0.0 and
// exponent spelling cannot be reconstructed by JavaScript JSON.stringify.
export function originalNodes(raw, fail) {
  let at = 0;
  const invalid = () => fail('ADMIN_FUNCTIONAL_ORIGINAL_JSON_INVALID', 409);
  function read(depth) {
    if (depth > 60) invalid();
    const start = at, children = Object.create(null);
    if (raw[at] === '{' || raw[at] === '[') {
      const isObject = raw[at++] === '{', end = isObject ? '}' : ']';
      const value = isObject ? Object.create(null) : []; let ordinal = 0;
      if (raw[at] !== end) while (true) {
        let key;
        if (isObject) {
          const name = read(depth + 1);
          if (typeof name.value !== 'string' || Object.hasOwn(children, name.value) || raw[at++] !== ':') invalid();
          key = name.value;
        } else key = ordinal++;
        const child = read(depth + 1); value[key] = child.value; children[key] = child;
        if (raw[at] === end) break;
        if (raw[at++] !== ',') invalid();
      }
      if (raw[at++] !== end) invalid();
      return { value, raw: raw.slice(start, at), children };
    }
    if (raw[at] === '"') {
      for (at++; at < raw.length; at++) {
        if (raw[at] === '\\') at++;
        else if (raw[at] === '"') { at++; break; }
      }
    } else {
      const match = /^(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/.exec(raw.slice(at));
      if (!match) invalid(); at += match[0].length;
    }
    let value;
    try { value = JSON.parse(raw.slice(start, at)); } catch { invalid(); }
    if (typeof value === 'number' && !Number.isFinite(value) || typeof value === 'string' && /[\uD800-\uDFFF]/u.test(value)) invalid();
    return { value, raw: raw.slice(start, at), children };
  }
  const node = read(0);
  if (at !== raw.length || !object(node.value)) invalid();
  return node;
}

export async function validateFunctionalPacket(facts, manifest, { canonical, fail, asOf = null } = {}) {
  const check = (condition, code = 'ADMIN_FUNCTIONAL_PACKET_INVALID') => { if (!condition) fail(code, 409); };
  const same = (a, b) => canonical(a) === canonical(b);
  const deep = (a, b) => {
    if (a === b) return true;
    if (Array.isArray(a)) return Array.isArray(b) && a.length === b.length && a.every((v, i) => deep(v, b[i]));
    return object(a) && object(b) && Object.keys(a).length === Object.keys(b).length && Object.keys(a).every(k => Object.hasOwn(b, k) && deep(a[k], b[k]));
  };
  const hash = async raw => [...new Uint8Array(await crypto.subtle.digest('SHA-256', encoder.encode(raw)))].map(v => v.toString(16).padStart(2, '0')).join('');
  const digest = value => hash(canonical(value));
  const key = async (prefix, value) => prefix + '-' + (await digest(value)).slice(0, 40);
  const keys = (value, fields) => object(value) && same(Object.keys(value).sort(), [...fields].sort());
  const rawObject = (node, excluded = [], included = null) => '{' + Object.keys(node.children).filter(k => !excluded.includes(k) && (included === null || included.includes(k))).sort().map(k => canonical(k) + ':' + node.children[k].raw).join(',') + '}';
  const rawHash = (node, excluded) => hash(rawObject(node, excluded));
  const nodeAt = (node, path) => path.reduce((n, part) => n?.children?.[part], node);
  const wrapperCache = new Map();
  async function original(wrapper, expected = null) {
    check(keys(wrapper, ['contract', 'canonical_json', 'sha256']) && wrapper.contract === 'frozen-original-json-v1' && HASH.test(wrapper.sha256 ?? '') && typeof wrapper.canonical_json === 'string' && encoder.encode(wrapper.canonical_json).length <= 1024 * 1024, 'ADMIN_FUNCTIONAL_ORIGINAL_INVALID');
    check(expected === null || expected === wrapper.sha256, 'ADMIN_FUNCTIONAL_ORIGINAL_CHANGED');
    const cached = wrapperCache.get(wrapper.canonical_json);
    if (cached) { check(cached.hash === wrapper.sha256, 'ADMIN_FUNCTIONAL_ORIGINAL_CHANGED'); return cached; }
    check(await hash(wrapper.canonical_json) === wrapper.sha256, 'ADMIN_FUNCTIONAL_ORIGINAL_CHANGED');
    const node = originalNodes(wrapper.canonical_json, fail); node.hash = wrapper.sha256;
    wrapperCache.set(wrapper.canonical_json, node); return node;
  }
  async function recordMaps(value, revisions = null) {
    check(object(value)); const maps = Object.create(null);
    for (const [table, rows] of Object.entries(value)) {
      check(object(rows)); maps[table] = Object.create(null);
      for (const [rid, frozen] of Object.entries(rows)) {
        check(ID.test(rid)); maps[table][rid] = await original(frozen, revisions ? revisions[table]?.[rid] ?? 'missing' : null);
      }
    }
    return maps;
  }
  const source = route => route === 'meta-ads' ? 'meta' : route?.startsWith('amazon-') ? 'amazon' : route === 'flipkart-search' ? 'flipkart' : ['blinkit', 'zepto', 'instamart'].includes(route) ? route : route === 'google-trends' ? 'trends' : ['company-pages', 'google-suppliers', 'google-maps', 'tradeindia', 'indiamart'].includes(route) ? 'supplier' : null;
  const publicUrl = value => {
    if (value === null) return null;
    check(typeof value === 'string' && value.length <= 4096 && !/[\x00-\x20\x7f\\]/.test(value));
    let url; try { url = new URL(value); } catch { check(false); }
    check(['http:', 'https:'].includes(url.protocol) && url.hostname && !url.username && !url.password && !url.hostname.includes('%') && ![...url.searchParams.keys()].some(k => /^(token|access_token|api_token|api_key|apikey|password|secret|client_secret|session_token|authorization)$/i.test(k)));
    return value;
  };
  const displayUrl = value => { try { return publicUrl(value); } catch { return null; } };
  function context(table, id, row) {
    const c = { table, id, source: row.source ?? null };
    if (table === 'trends_evidence') { const meta = row.parsed?.metadata ?? {}; Object.assign(c, {source: 'trends', observed_at: meta.collected_at ?? null, request: meta, original_reference: meta.source_reference ?? null}); }
    else if (table === 'source_checks') Object.assign(c, {observed_at: row.at ?? null, route: row.route ?? null, original_reference: row.reference ?? null, request_hash: row.request_hash ?? null, query: row.query ?? null, source: source(row.route), acquisition_id: object(row.envelope) ? row.envelope.acquisition_id ?? null : row.acquisition_id ?? null});
    else {
      Object.assign(c, {observed_at: row.observed_at ?? null, acquisition_id: row.acquisition_id ?? null});
      if (table === 'captures') Object.assign(c, {original_reference: row.reference ?? null, request: row.request ?? (object(row.reference) ? row.reference.request ?? null : null), route: object(row.reference) ? row.reference.route ?? null : row.route ?? null, capture_hash: row.capture_hash ?? null});
      else { const f = row.facts ?? {}; Object.assign(c, {capture_id: row.capture_id ?? null, source_item: row.source_item ?? null, semantic_id: row.semantic_id ?? null, location: f.location ?? null, query: f.query ?? null, route: f.source_route ?? (row.source === 'meta' ? 'meta-ads' : ['blinkit','zepto','instamart'].includes(row.source) ? row.source : ({bestsellers:'amazon-bestsellers','movers-and-shakers':'amazon-movers-and-shakers',product_search:row.source === 'amazon' ? 'amazon-search' : 'flipkart-search','product-search':row.source === 'amazon' ? 'amazon-search' : 'flipkart-search'})[f.list_type] ?? null), original_reference: f.url || row.semantic?.product_url || null}); }
    }
    return c;
  }
  async function scoped(rid, node, records, meaning, cid, at) {
    const row = node.value;
    check(row.kind === 'scoped-evidence-v1' && row.record_hash === await rawHash(node, ['record_hash']) && rid === 'scoped-evidence-' + (await hash(rawObject(node, ['reviewed_at', 'meaning_basis', 'record_hash', 'binding_key', 'revision', 'previous_binding_id']))).slice(0, 40), 'ADMIN_FUNCTIONAL_SCOPE_CHANGED');
    check(row.accepted === true && row.concept_id === cid && same(row.subject, {kind:'concept',concept_id:cid,definition_hash:meaning}) && row.meaning_basis === 'accepted_definition' && source(row.route) === row.source && utc(row.reviewed_at, fail) <= at, 'ADMIN_FUNCTIONAL_SCOPE_INVALID');
    const allowed = {narrow_type:['exact_product','relevant_family'],broader_context:['broader_context'],example_offer:['exact_product','relevant_family','example_offer'],supplier_company:['relevant_family','broader_context'],supplier_offer:['exact_product','relevant_family','example_offer']};
    check(allowed[row.scope]?.includes(row.relationship) || ['unclear','unrelated'].includes(row.relationship));
    check(row.admissibility === (['unclear','unrelated'].includes(row.relationship) ? row.relationship : 'accepted'));
    const binding = {kind:row.kind,subject:row.subject,method:row.method,role:row.job_proof?.role??'operator',role_contract:row.job_proof?.prompt_version??'explicit-source-scope-v1',references:row.references,route:row.route,scope:row.scope,question:row.question,selected_fields:row.usable_fields.map(field=>pick(field,['reference','path','hash']))};
    check(integer(row.revision,1) && row.binding_key===await key('scope-binding',binding),'ADMIN_FUNCTIONAL_SCOPE_BINDING_CHANGED');
    const contexts = [];
    for (const ref of row.references) {
      const n = records[ref.table]?.[ref.id]; check(n && n.hash === ref.hash, 'ADMIN_FUNCTIONAL_SCOPE_ORIGINAL_CHANGED');
      const c = context(ref.table, ref.id, n.value); contexts.push(c);
      check(utc(c.observed_at, fail) <= utc(row.reviewed_at, fail) && c.source === row.source && (c.route == null || c.route === row.route));
      if (ref.table === 'observations') { const capture = records.captures?.[n.value.capture_id]?.value; check(capture && ['source','acquisition_id','observed_at'].every(k => deep(capture[k], n.value[k]))); }
    }
    check(deep(contexts, row.source_contexts), 'ADMIN_FUNCTIONAL_SCOPE_CONTEXT_CHANGED');
    for (const field of row.usable_fields) {
      const n = records[field.reference.table]?.[field.reference.id], value = nodeAt(n, field.path);
      check(value && row.references.some(r => r.table === field.reference.table && r.id === field.reference.id) && deep(value.value, field.value) && await hash(value.raw) === field.hash, 'ADMIN_FUNCTIONAL_SCOPE_FIELD_CHANGED');
    }
    if(row.support!=='insufficient'){
      const requestKnown=Object.keys(row.request_dependencies).length||Object.values(row.job_proof?.capture_contexts??{}).some(p=>p.context.state==='verified'&&object(p.context.request)&&Object.keys(p.context.request).length)||contexts.some(c=>object(c.request)&&Object.keys(c.request).length||text(c.query));
      check(row.usable_fields.length&&requestKnown&&row.usable_fields.some(f=>(f.reference.table==='observations'&&['semantic','facts'].includes(f.path[0])||f.reference.table==='trends_evidence'&&deep(f.path.slice(0,2),['parsed','series'])||f.reference.table==='source_checks'&&f.path[0]==='candidates')&&f.value!==null&&f.value!==''&&(!Array.isArray(f.value)||f.value.length)&&(!object(f.value)||Object.keys(f.value).length)),'ADMIN_FUNCTIONAL_SCOPE_SUPPORT_CHANGED');
    }
    return row;
  }
  async function definition(node, rid) {
    const record=node.value;
    check(record.kind === 'functional-definition-v1' && keys(record, [...DEFINITION_INTENT,'revision','previous_definition_id','previous_meaning_hash','record_hash']) && keys(record.definition, DEFINITION) && record.accepted === true && integer(record.revision,1));
    check(DEFINITION.every(k => typeof record.definition[k] === (['report_title','primary_use','mechanism'].includes(k) ? 'string' : 'object')));
    check(await rawHash(node,['record_hash']) === record.record_hash && await digest(pick(record.definition,MEANING)) === record.meaning_hash && await digest({report_title:record.definition.report_title}) === record.presentation_hash && rid==='functional-definition-'+(await hash(rawObject(node,[],DEFINITION_INTENT))).slice(0,40),'ADMIN_FUNCTIONAL_DEFINITION_CHANGED');
    await validateDefinitionAcceptance(node,{asOf,fail});
  }
  async function examples(product) {
    const proof = product.functional_definition_proof, d = product.functional_definition;
    check(object(proof) && keys(proof,['id','record_hash','record']) && object(d));
    const wrapped=proof.record?.contract==='frozen-original-json-v1';
    const node=wrapped?await original(proof.record):originalNodes(canonical(proof.record),fail);
    node.hash??=await hash(node.raw);const record=node.value;
    check(wrapped===(object(record.job_proof)&&Object.hasOwn(record.job_proof,'definition_acceptance')),'ADMIN_FUNCTIONAL_DEFINITION_CHANGED');
    await definition(node, proof.id);
    const expected = {concept_id:product.concept_id,definition_id:proof.id,revision:record.revision,meaning_hash:record.meaning_hash,presentation_hash:record.presentation_hash,accepted:true,basis:'accepted_definition',...record.definition};
    check(same(d,expected) && product.title===d.report_title && product.functional_definition_hash === await digest(expected) && proof.record_hash === record.record_hash && product.source_revisions?.source_checks?.[proof.id]===node.hash && record.concept_id === product.concept_id && utc(record.reviewed_at,fail) <= utc(product.example_as_of,fail),'ADMIN_FUNCTIONAL_DEFINITION_CHANGED');
    const coverage = product.example_coverage, rows = product.example_products, bindings = product.example_bindings;
    check(keys(coverage,['retained_current_offers','selected_offers','omitted_offers','limit','rejected_observations']) && ['retained_current_offers','selected_offers','omitted_offers','limit'].every(k => integer(coverage[k])) && coverage.limit <= 20 && Array.isArray(rows) && rows.length === coverage.selected_offers && rows.length === Math.min(coverage.retained_current_offers,coverage.limit) && coverage.retained_current_offers === rows.length + coverage.omitted_offers && Array.isArray(coverage.rejected_observations) && object(bindings));
    check(same(Object.keys(bindings).sort(),[...new Set(rows.map(r=>r.membership_id))].sort()));
    const reviewProofs=product.example_review_proofs??{},wantedReviews=[...new Set(Object.values(bindings).filter(b=>Object.hasOwn(b.record??{},'example_review')).map(b=>b.record.example_review.id))].sort(),reviewNodes={};
    check(object(reviewProofs)&&same(Object.keys(reviewProofs).sort(),wantedReviews)&&(!Object.hasOwn(product,'example_review_proofs')||wantedReviews.length>0),'ADMIN_FUNCTIONAL_EXAMPLE_CHANGED');
    for(const [rid,wrapper]of Object.entries(reviewProofs))reviewNodes[rid]=await original(wrapper,product.source_revisions?.source_checks?.[rid]??'missing');
    if(Object.hasOwn(product,'example_selection')){const selection=product.example_selection;check(keys(selection,['requested_limit','byte_limit','omitted_for_byte_limit'])&&integer(selection.requested_limit,1,20)&&selection.requested_limit>coverage.limit&&selection.byte_limit===1048576&&integer(selection.omitted_for_byte_limit,1)&&selection.omitted_for_byte_limit===Math.min(coverage.retained_current_offers,selection.requested_limit)-rows.length,'ADMIN_FUNCTIONAL_EXAMPLE_CHANGED');}
    const exampleFields=['recommendation_contract','functional_definition','functional_definition_hash','functional_definition_proof','example_products','example_bindings','example_as_of','example_coverage',...['example_review_proofs','example_selection'].filter(f=>Object.hasOwn(product,f))];
    check(encoder.encode(canonical(pick(product,exampleFields))).length<=1048576,'ADMIN_FUNCTIONAL_EXAMPLE_SIZE_LIMIT');
    const offers = new Set(), ids = new Set();
    for (const row of rows) {
      const b = bindings[row.membership_id], m = b?.record;
      check(object(m) && m.kind === 'concept-example-v1' && m.accepted === true && m.concept_id === product.concept_id && m.meaning_hash === d.meaning_hash && m.meaning_basis === 'accepted_definition' && m.record_hash === b.membership_hash && m.record_hash === await digest(omit(m,['record_hash'])) && product.source_revisions?.source_checks?.[row.membership_id]===await digest(m) && row.membership_id === await key('concept-example',omit(m,['prepared_at','record_hash'])) && utc(m.prepared_at,fail) <= utc(product.example_as_of,fail),'ADMIN_FUNCTIONAL_EXAMPLE_CHANGED');
      check((m.method==='bounded_B_example_review')===Object.hasOwn(m,'example_review'),'ADMIN_FUNCTIONAL_EXAMPLE_CHANGED');
      if(Object.hasOwn(m,'example_review')){const ref=m.example_review;check(keys(ref,['id','record_hash'])&&reviewNodes[ref.id],'ADMIN_FUNCTIONAL_EXAMPLE_CHANGED');const review=await validateExampleBinding(reviewNodes[ref.id],ref.id,{...m,record_hash:ref.record_hash},{asOf:asOf===null||utc(product.example_as_of,fail)<=utc(asOf,fail)?product.example_as_of:asOf,fail}),judgment=record.example_decisions[m.child_id],bound=review.binding.children[m.child_id];check(!judgment||judgment.association_hash!==bound.association_hash||judgment.description_hash!==bound.description_hash,'ADMIN_FUNCTIONAL_EXAMPLE_CHANGED');}
      const node = await original(row.observation,row.observation_hash), observation = node.value, f = observation.facts, s = observation.semantic;
      check(m.observation_hashes?.[row.observation_id] === node.hash && observation.semantic_id === m.semantic_id && utc(observation.observed_at,fail) <= utc(product.example_as_of,fail));
      const url = [f.url,s.product_url,s.link_url].find(v=>typeof v === 'string' && v) ?? null;
      let pack = text(f.quantity), price = null;
      if (object(f.price)) { const q = f.price; if (text(q.displayed)) price = {kind:'retail',displayed:q.displayed,currency:text(q.currency),basis:text(q.basis),source_url:publicUrl(q.source_url || url)}; pack = text(q.pack_basis) || pack; }
      if (['unknown','not_stated','not_verified'].includes(pack)) pack = null;
      const images = [...(f.image_urls ?? []), ...(s.original_image_url ? [s.original_image_url] : [])].filter(v=>v!==null).map(publicUrl);
      const expectedRow = {id:await key('report-example',[row.membership_id,row.observation_id]),membership_id:row.membership_id,membership_hash:m.record_hash,child_id:m.child_id,meaning_hash:d.meaning_hash,observation_id:row.observation_id,observation_hash:node.hash,observation:row.observation,source:observation.source,title:text(s.title),seller:text(f.seller_name)||text(f.seller),advertiser:text(f.advertiser_name),url:publicUrl(url),observed_at:observation.observed_at,pack,price,image_urls:[...new Set(images)].sort()};
      const location = pick(f,['location','location_context','reference_location'].filter(k=>Object.hasOwn(f,k)));
      const offer = canonical([observation.source,text(observation.source==='amazon'?f.asin:f.product_id)||observation.source_item,location]);
      check(expectedRow.title && same(row,expectedRow) && !ids.has(row.id) && !offers.has(offer),'ADMIN_FUNCTIONAL_EXAMPLE_DISPLAY_CHANGED'); ids.add(row.id); offers.add(offer);
    }
  }

  check(facts?.schema_version === 3 && facts.recommendation_contract === 'functional-type-examples-v1' && manifest?.schema_version === 2 && manifest.recommendation_contract === facts.recommendation_contract && manifest.workbook_contract === 'fit-checklist-examples-v2','ADMIN_FUNCTIONAL_TUPLE_INVALID');
  check(Array.isArray(facts.products) && facts.products.length <=20);
  for (const product of facts.products) { await examples(product); const supplier=await fit(product); const checked=await market(product,supplier); await photo(product); await routeFacts(product,checked); }
  await assets();
  for (const language of manifest.languages) await writing(language);
  return facts;

  async function fit(product) {
    const inputNode = await original(product.functional_fit_input, product.functional_fit_input_hash), input = inputNode.value;
    const aNode = await original(product.functional_fit_assessment,product.source_revisions?.assessments?.[product.concept_id]??'missing'), a = aNode.value, proof = product.functional_fit_proof;
    check(a.record_hash === await rawHash(aNode,['record_hash']) && a.contract_version === 'research-e-fit10-functional-v4' && a.fit_policy_id === 'fit-10-v1' && a.input_hash === inputNode.hash && aNode.children.input.raw === inputNode.raw && a.functional_proof_hash === product.functional_fit_proof_hash && deep(a.functional_proof,proof) && deep(a.result?.criteria,product.criteria) && a.result?.compliance_note === product.compliance_note,'ADMIN_FUNCTIONAL_FIT_ASSESSMENT_CHANGED');
    check(product.functional_fit_proof_hash === await digest(proof) && input.id === product.concept_id && input.functional_contract === 'research-e-fit10-functional-v4' && input.meaning_hash === product.functional_definition.meaning_hash && deep(input.description,product.fit_description) && keys(input.description,FIT_FIELDS) && product.fit_description_contract === 'functional-type-inference-v1' && await digest(pick(input.description,MEANING)) === input.meaning_hash,'ADMIN_FUNCTIONAL_FIT_INPUT_CHANGED');
    const refs = product.functional_fit_reference_proofs, records = await recordMaps(refs.records,product.source_revisions), scopedRows = await recordMaps({source_checks:refs.scoped_records},product.source_revisions);
    const dNode=records.source_checks?.[proof.definition_id],d = dNode?.value;
    check(d && d.record_hash === proof.definition_record_hash && d.meaning_hash === input.meaning_hash); await definition(dNode,proof.definition_id);
    const selectedDefinition=product.functional_definition_proof.record.contract==='frozen-original-json-v1'?(await original(product.functional_definition_proof.record)).value:product.functional_definition_proof.record;
    const features = new Set(), audiences = new Set();
    for (const [rid,entries] of Object.entries(proof.examples)) for (const e of entries) {
      const association = records.associations?.[e.child_id], descriptionRecord = records.descriptions?.[e.semantic_id];
      let desc = null, descriptionNode = null;
      if (descriptionRecord) {
        const result = descriptionRecord.value.result;
        for (const [index, candidate] of (result?.products ?? []).entries()) if (await key('child',[e.semantic_id,index,await digest(result)]) === e.child_id) {
          desc=candidate; descriptionNode=descriptionRecord.children.result.children.products.children[index]; break;
        }
      }
      check(association?.hash === e.association_hash && association.value.status === 'resolved' && association.value.concept_id === product.concept_id && association.value.semantic_id === e.semantic_id && desc,'ADMIN_FUNCTIONAL_FIT_EXAMPLE_CHANGED');
      check(await hash(descriptionNode.raw) === e.description_hash);
      const judgment = d.example_decisions[e.child_id];
      if(Object.hasOwn(e,'example_review_id')||Object.hasOwn(e,'example_review_hash')){const review=records.source_checks?.[e.example_review_id],current=selectedDefinition.example_decisions[e.child_id];check(review&&(!current||current.association_hash!==e.association_hash||current.description_hash!==e.description_hash),'ADMIN_FUNCTIONAL_FIT_EXAMPLE_CHANGED');await validateExampleBinding(review,e.example_review_id,{...e,concept_id:product.concept_id,meaning_hash:input.meaning_hash,record_hash:e.example_review_hash},{asOf:asOf===null||utc(product.example_as_of,fail)<=utc(asOf,fail)?product.example_as_of:asOf,fail});}
      else if (judgment) check(judgment.decision==='same' && judgment.association_hash===e.association_hash && judgment.description_hash===e.description_hash);
      else { const raw=product.description; check(await digest({primary_use:raw.function,mechanism:raw.mechanism,use_context:[],compatibility_constraints:[],included_distinctions:raw.features,excluded_distinctions:[]})===input.meaning_hash); }
      const projection = pick(desc,['function','mechanism','features','audience']); check(rid === await key('functional-role-example',[product.concept_id,input.meaning_hash,projection]));
      for (const [obs, expectedHash] of Object.entries(e.observation_hashes)) { const o=records.observations?.[obs]; check(o?.hash===expectedHash && o.value.semantic_id===e.semantic_id && utc(o.value.observed_at,fail)<=utc(product.example_as_of,fail)); }
      for (const feature of projection.features ?? []) features.add(feature); if (projection.audience) audiences.add(projection.audience);
    }
    check(same([...features].sort(),input.description.example_features) && same([...audiences].sort(),input.description.example_audiences));
    const supplied = Object.fromEntries(input.observations.map((row,index)=>[row.id,{row,node:inputNode.children.observations.children[index]}]));
    check(Object.keys(supplied).length===input.observations.length && same(Object.keys(supplied).sort(),Object.keys(proof.scoped_facts).sort()));
    const usedScoped = new Set();
    for (const [rid, facts] of Object.entries(proof.scoped_facts)) {
      const selected=supplied[rid], st=selected.row.source_text;
      check(rid==='functional-fit-fact-'+(await hash('['+canonical(product.concept_id)+','+canonical(input.meaning_hash)+','+selected.node.children.source_text.raw+']')).slice(0,40));
      for (const f of facts) {
        const node=scopedRows.source_checks[f.scoped_evidence_id]; check(node); const row=await scoped(f.scoped_evidence_id,node,records,input.meaning_hash,product.concept_id,utc(product.example_as_of,fail)); usedScoped.add(f.scoped_evidence_id);
        const originalValue=nodeAt(records[f.reference.table]?.[f.reference.id],f.path);
        check(row.record_hash===f.scoped_evidence_hash && ['narrow_type','example_offer','supplier_offer'].includes(row.scope) && row.admissibility==='accepted' && row.scope===st.scope && same(row.limitations,st.limitations) && PHYSICAL[f.path.at(-1)]===st.field && originalValue && deep(originalValue.value,st.value) && await hash(originalValue.raw)===f.value_hash);
      }
    }
    check(same([...usedScoped].sort(),Object.keys(scopedRows.source_checks).sort()));
    check(Array.isArray(product.criteria) && same(product.criteria.map(r=>r.id),FIT_IDS));
    for (const row of product.criteria) {
      check(['Meets','Does not meet','Unclear','Not assessed'].includes(row.state) && ['observed','inferred','not_stated','conflicting'].includes(row.basis) && text(row.reason) && Array.isArray(row.references) && new Set(row.references).size===row.references.length && row.references.every(r=>Object.hasOwn(supplied,r)));
      if (row.description_inference!==null && row.description_inference!==undefined) {
        const m=row.description_inference; check(keys(m,['kind','meaning_hash','fields']) && m.kind==='functional-type-inference-v1' && m.meaning_hash===input.meaning_hash && row.basis==='inferred' && ['Meets','Does not meet'].includes(row.state) && row.references.length===0 && row.reason.startsWith('Estimated:') && m.fields.length>0);
        for (const field of m.fields) check(FIT_FIELDS.includes(field.field) && input.description[field.field] && field.hash===await digest(input.description[field.field]));
      } else if (['Meets','Does not meet'].includes(row.state)) check(['observed','inferred'].includes(row.basis) && row.references.length>0);
    }
    const states=product.criteria.map(r=>r.state), matches=states.filter(s=>s==='Meets').length;
    check(same(states,product.kpi_states) && matches===product.kpi_matches && matches>=5 && product.fit_policy_id==='fit-10-v1' && same(product.criterion_ids,FIT_IDS) && product.kpi_total===10 && product.fit_tier===(matches>=6?'good':'decent'));
    const writing=product.criteria.filter(r=>r.state==='Meets'&&['observed','inferred'].includes(r.basis)).slice(0,6).map(r=>({text:r.reason,basis:r.basis,references:r.references,...(r.description_inference?{description_inference:r.description_inference}:{})}));
    check(same(writing,product.writing_facts) && ['prices','retail_price_observations','supplier_price_observations'].every(k=>same(product[k],[])),'ADMIN_FUNCTIONAL_DISPLAY_FACTS_CHANGED');
    return suppliers(product);
  }

  async function suppliers(product, preview = false) {
    const proof=product.functional_supplier_proof;
    if(proof?.contract==='functional-supplier-scoped-proof-v2') {
      check(!preview,'ADMIN_FUNCTIONAL_SCOPED_SUPPLIER_INVALID');
      const legacy=await suppliers({...product,functional_supplier_proof:proof.legacy_context},true);
      return scopedSuppliers(product,legacy,{check,keys,same,deep,digest,key,recordMaps,rawHash,hash,canonical,
        utc:value=>utc(value,fail),displayUrl,publicUrl,supplierEffortRecord,definition,model:'gpt-6-luna',text,integer,ID,HASH});
    }
    check(keys(proof,['contract','as_of','records']) && proof.contract==='functional-supplier-context-v1' && proof.as_of===product.example_as_of && keys(proof.records,['supplier_assessments','source_checks']));
    const records=await recordMaps(proof.records,product.source_revisions);
    if(!preview)check(same(Object.keys(records.supplier_assessments).sort(),product.supplier_assessment_ids) && same(Object.keys(records.source_checks).sort(),product.supplier_search_check_ids));
    const at=utc(proof.as_of,fail),wanted=pick({...product.description,concept_id:product.concept_id},['concept_id','label','category','function','mechanism','features']);
    const latest=new Map(),excluded=[],accepted=[];
    for(const [rid,node] of Object.entries(records.supplier_assessments)){
      const r=node.value,input=r.input;
      check(r.version==='supplier-terms-v1'&&deep(pick(input.product,Object.keys(wanted)),wanted)&&r.input_hash===await hash(node.children.input.raw)&&r.result.id===input.id&&utc(r.checked_at,fail)<=at,'ADMIN_FUNCTIONAL_SUPPLIER_CHANGED');
      if(r.source_dates!==undefined&&r.source_dates!==null)check(same(Object.keys(r.source_dates).sort(),input.pages.map(p=>p.id).sort())&&Object.values(r.source_dates).every(d=>utc(d,fail)<=at),'ADMIN_FUNCTIONAL_SUPPLIER_CHANGED');
      check(!latest.has(input.id),'ADMIN_FUNCTIONAL_SUPPLIER_CHANGED');latest.set(input.id,{rid,row:r});
    }
    const limitation='This dated supplier lead still needs matching to this product type.';
    for(const [lead,{rid,row:r}] of [...latest].sort(([a],[b])=>a<b?-1:a>b?1:0)){
      const relationship=r.result.relevance.relationship;
      if(!['exact_product','relevant_company'].includes(relationship)){excluded.push({id:lead,relationship,reason:r.result.relevance.reason});continue;}
      const pages=r.input.pages,page=r.result.relevance.references.map(id=>pages.find(p=>p.id===id)).find(Boolean)??pages[0],terms=r.result.terms;
      const fields=Object.fromEntries(terms.map(t=>[t.field,t])),stated=(field,value)=>fields[field]?.basis==='observed'&&fields[field]?.value===value;
      const direct=stated('direct_customer_dispatch','Yes'),bulk=stated('bulk_stock_prerequisite','Yes'),single=stated('single_piece_purchase','Yes')||fields.purchase_moq?.basis==='observed'&&/^1\s+(piece|pieces|unit|units)$/i.test(fields.purchase_moq.value);
      const capability=bulk?2:single&&direct?0:1,subscription=fields.subscription_requirement,fee=subscription?.basis==='observed'&&subscription?.value==='Not required'?0:subscription?.value==='Required'?2:1;
      accepted.push({rid,row:r,rank:[relationship==='exact_product'?0:1,capability,fee,lead],display:{lead_id:lead,name:new URL(page.url).hostname||page.title,url:displayUrl(page.url),single_piece_dispatch:'not_stated',subscription:'not_stated',min_order:null,ecommerce_support:'Not stated',reason:limitation,relationship:'unclear',supply_arrangement:limitation,
        terms:terms.map(t=>({field:t.field,value:'Not stated',basis:'not_stated',references:[],quotes:[],reason:limitation})),reviewed_at:r.checked_at,source_capture_at:r.source_dates&&Object.keys(r.source_dates).length?Object.values(r.source_dates).sort((a,b)=>utc(a,fail)-utc(b,fail))[0]:null,id:lead,relevance:{relationship:'unclear',reason:limitation,references:[]},type_scope_limitation:limitation}});
    }
    const leads=[...accepted].sort((a,b)=>{for(let i=0;i<4;i++){if(a.rank[i]<b.rank[i])return -1;if(a.rank[i]>b.rank[i])return 1;}return 0;}).map(r=>r.display),empty=[],effortRows=[],actual=new Map();
    for(const [rid,node] of Object.entries(records.source_checks)){
      const r=node.value;
      if(r.kind==='supplier-effort-v1'&&r.concept_id===product.concept_id){await supplierEffortRecord(r);if(deep(r.sourcing_scope,wanted)&&!retiredSupplierRoutes.includes(supplierRouteAliases[r.route]??r.route)&&utc(r.at,fail)<=at){effortRows.push([rid,r]);for(const ref of r.receipt_refs)if(ref.proof!=='none'&&utc(ref.attempt_started_at,fail)<=at&&utc(ref.checked_at,fail)<=at)actual.set(canonical([ref.id,ref.hash]),{...ref,check_id:rid,pass_id:r.pass_id});}}
      if(r.source==='supplier'&&r.concept_id===product.concept_id&&r.description_hash===await digest(product.description)&&r.state==='usable'&&r.supplier_search_status==='complete'&&r.candidate_count===0&&Number.isInteger(r.candidate_count)&&r.at&&utc(r.at,fail)<=at)empty.push(rid);
    }
    effortRows.sort((a,b)=>utc(a[1].at,fail)-utc(b[1].at,fail)||(a[0]<b[0]?-1:a[0]>b[0]?1:0));
    const attempts=[...actual.values()].sort((a,b)=>utc(a.attempt_started_at,fail)-utc(b.attempt_started_at,fail)||(a.id<b.id?-1:a.id>b.id?1:0)||(a.hash<b.hash?-1:a.hash>b.hash?1:0)),assessmentIds=[...latest.values()].map(r=>r.rid).sort();
    const effort={check_ids:effortRows.map(([id])=>id).sort(),actual_attempt_refs:attempts,last_attempt_at:attempts.map(r=>r.attempt_started_at).sort((a,b)=>utc(a,fail)-utc(b,fail)).at(-1)??null,internal_outcome:accepted.length?'usable_leads':currentSupplierOutcome(effortRows,fail),effort_satisfied:!!attempts.length,usable_lead_count:accepted.length,legacy_interpretation_ids:assessmentIds,legacy_search_check_ids:empty.sort()};
    const satisfied=!!(latest.size||empty.length||attempts.length),status=!satisfied?'not_started':latest.size||empty.length?'complete':['failed','blocked'].includes(effort.internal_outcome)?'blocked':'attempted';
    const expected={suppliers:leads,supplier_limitation:leads.length?'These dated supplier leads still need matching to this product type.':'Supplier search did not produce any usable results.',supplier_search_status:status,supplier_leads_checked:latest.size,supplier_leads_found:leads.length,supplier_decisions:excluded,supplier_effort_satisfied:satisfied,supplier_effort:effort,supplier_search_check_ids:[...new Set([...empty,...effort.check_ids])].sort(),supplier_assessment_ids:assessmentIds};
    check((preview||same(pick(product,Object.keys(expected)),expected))&&same(Object.keys(records.source_checks).sort(),expected.supplier_search_check_ids),'ADMIN_FUNCTIONAL_SUPPLIER_DISPLAY_CHANGED');
    return preview?{accepted,expected}:{legacy:accepted,source_refs:[]};
  }

  async function photo(product) {
    const proof=product.functional_photo_proof, provenance=product.image_provenance;
    check(keys(proof,['contract','records'])&&proof.contract==='functional-photo-proof-v1'&&keys(proof.records,['source_checks']),'ADMIN_FUNCTIONAL_PHOTO_PROOF_INVALID');
    const originals=await recordMaps(proof.records,product.source_revisions), records=originals.source_checks;
    if(provenance===undefined||provenance===null){check(!Object.keys(records).length&&product.image_path===null,'ADMIN_FUNCTIONAL_PHOTO_PROOF_INVALID');return;}
    const node=records[provenance.id], record=node?.value, market=product.functional_market_proof;
    check(record&&market.matches[provenance.source_ref],'ADMIN_FUNCTIONAL_PHOTO_PROOF_INVALID');
    const observation=(await original(market.records.observations[provenance.source_ref],product.source_revisions.observations[provenance.source_ref])).value;
    const match=market.matches[provenance.source_ref], known=[...(observation.facts.image_urls??[]),...(observation.semantic.original_image_url?[observation.semantic.original_image_url]:[])];
    const binding={concept_id:product.concept_id,description_hash:await digest(product.description),observation_id:provenance.source_ref,source_url:publicUrl(provenance.source_url),observed_at:observation.observed_at,relationship:match.relationship};
    check(match.scope==='narrow_type'&&['exact_product','relevant_family'].includes(match.relationship)&&known.includes(binding.source_url)&&['public-product-image-v1','public-product-image-v2','public-product-image-v3'].includes(record.version)&&record.reference===provenance.id&&await key('product-image',[record.version,binding,...(record.version==='public-product-image-v3'?[record.physical_acquisition_id]:[])])===provenance.id&&record.kind==='public_product_image'&&record.source==='product_photo'&&record.route==='public_image_http'&&record.state==='usable'&&record.model_calls===0&&record.apify_usd==='0'&&Object.keys(binding).every(k=>deep(record[k],binding[k]))&&utc(record.at,fail)<=utc(product.example_as_of,fail)&&record.receipt?.checked_at===record.at&&record.receipt?.state==='usable'&&record.receipt?.requested_url===binding.source_url,'ADMIN_FUNCTIONAL_PHOTO_PROOF_INVALID');
    const image=record.image;
    check(image?.source_kind==='public_product_image'&&['JPEG','PNG','WEBP'].includes(image.format)&&['width','height','bytes'].every(k=>integer(image[k],1))&&image.bytes<=5*1024*1024&&HASH.test(image.sha256)&&same(record.receipt.metadata,pick(image,['sha256','format','width','height','bytes'])),'ADMIN_FUNCTIONAL_PHOTO_PROOF_INVALID');
    const expectedIds=[provenance.id];
    if(record.version==='public-product-image-v2'){
      const shared=records[record.acquisition_ref], originalRow=shared?.value;
      check(shared&&originalRow.kind==='public-product-image-read-v1'&&originalRow.reference===record.acquisition_ref&&originalRow.source_url===binding.source_url&&originalRow.state==='usable'&&originalRow.record_hash===await rawHash(shared,['record_hash'])&&record.record_hash===await rawHash(node,['record_hash'])&&record.acquisition_ref===await key('product-image-read',['public-product-image-read-v1','GET',binding.source_url,{Accept:'image/jpeg,image/png,image/webp','Accept-Encoding':'identity'}])&&deep(originalRow.image,image)&&deep(originalRow.receipt,record.receipt)&&originalRow.at===record.at,'ADMIN_FUNCTIONAL_PHOTO_PROOF_INVALID');
      expectedIds.push(record.acquisition_ref);
    }
    if(record.version==='public-product-image-v3'){
      const shared=records[record.acquisition_ref],s=shared?.value,b=s?.acquisition_binding,r=s?.response_proof;
      const request={url:binding.source_url,transport:'public-http',headers:{Accept:'image/jpeg,image/png,image/webp','Accept-Encoding':'identity','User-Agent':'NeuraFlow-Public-Research/1.0'},options:{max_bytes:5*1024*1024,timeout_seconds:20}};
      check(shared&&s.kind==='public-product-image-read-v2'&&s.reference===record.acquisition_ref&&s.reference===await key('product-image-read',[s.kind,s.physical_acquisition_id])&&s.source_url===binding.source_url&&s.state==='usable'&&s.record_hash===await rawHash(shared,['record_hash'])&&record.record_hash===await rawHash(node,['record_hash'])&&deep(s.image,image)&&deep(s.receipt,record.receipt)&&s.at===record.at,'ADMIN_FUNCTIONAL_PHOTO_PROOF_INVALID');
      check(b&&b.route==='public_image_http'&&deep(b.request,request)&&b.method==='GET'&&b.acquisition_id===s.physical_acquisition_id&&s.acquisition_binding_hash===await digest(b)&&b.physical_request_fingerprint===await digest({contract:'physical-request-v1',route:'public_image_http',method:'GET',request,capture_options:b.capture_options})&&integer(b.generation,1,3)&&record.physical_acquisition_id===b.acquisition_id&&record.generation===b.generation,'ADMIN_FUNCTIONAL_PHOTO_PROOF_INVALID');
      const rn=shared.children.response_proof;
      check(r&&r.kind==='source-acquisition-stage-v1'&&r.stage_type==='response'&&r.acquisition_id===b.acquisition_id&&r.binding_hash===s.acquisition_binding_hash&&r.record_hash===await rawHash(rn,['record_hash'])&&s.original_response_id==='source-acquisition-stage-'+(await hash(rawObject(rn,['record_hash','recorded_at']))).slice(0,40)&&r.bridge_contract==='verified-image-response-v1'&&r.source_state==='usable'&&r.observed_at===record.at&&deep(r.fetch_receipt,record.receipt)&&r.body_sha256===image.sha256&&deep(r.image_failure,{classification:'usable',next_retry_at:null})&&record.failure_classification==='usable'&&record.next_retry_at===null,'ADMIN_FUNCTIONAL_PHOTO_PROOF_INVALID');
      expectedIds.push(record.acquisition_ref);
    }
    const expected={id:provenance.id,owner_id:product.concept_id,path:image.path,sha256:image.sha256,source_kind:'public_product_image',source_ref:binding.observation_id,source_url:binding.source_url,observed_at:binding.observed_at,relationship:binding.relationship};
    check(same(provenance,expected)&&product.image_path===image.path&&same(Object.keys(records).sort(),expectedIds.sort()),'ADMIN_FUNCTIONAL_PHOTO_PROOF_INVALID');
  }

  async function market(product,supplier) {
    const proof=product.functional_market_proof, meaning=product.functional_definition.meaning_hash;
    check(keys(proof,['contract','concept_id','meaning_hash','as_of','scope_hash','scoped_records','records','matches','source_signals','source_support']) && product.functional_market_contract==='functional-market-scope-v1' && proof.contract===product.functional_market_contract && proof.concept_id===product.concept_id && proof.meaning_hash===meaning && HASH.test(proof.scope_hash) && proof.as_of===product.example_as_of);
    const at=utc(proof.as_of,fail), records=await recordMaps(proof.records,product.source_revisions), rows=Object.create(null), used=Object.fromEntries(Object.keys(records).map(k=>[k,new Set()]));
    for (const [rid,frozen] of Object.entries(proof.scoped_records)) { const node=await original(frozen,product.source_revisions?.source_checks?.[rid]); rows[rid]=await scoped(rid,node,records,meaning,product.concept_id,at); for(const ref of rows[rid].references)used[ref.table].add(ref.id); }
    check(Object.keys(records).every(t=>same(Object.keys(records[t]).sort(),[...used[t]].sort())));
    const matched=Object.create(null);
    for (const [ref,m] of Object.entries(proof.matches)) {
      const row=rows[m.scope_review_id]; check(row && row.scope==='narrow_type' && row.admissibility==='accepted' && row.references.some(r=>r.id===ref) && same(m,{scope_review_id:m.scope_review_id,scope_review_hash:row.record_hash,relationship:row.relationship,scope:row.scope,reason:row.reason,limitations:row.limitations}));
      if(row.method==='functional_source_match_v4') { const p=row.job_proof,answer=p?.accepted_item; const r=answer?.relevance?.filter(r=>r.id===ref); check(p?.role==='E' && p.prompt_version==='research-e-source-support-v4' && p.accepted_item_hash===await digest(answer) && row.support==='insufficient' && r?.length===1 && same(r[0],{id:ref,relationship:row.relationship,scope:row.scope,reason:row.reason,limitations:row.limitations}),'ADMIN_FUNCTIONAL_SOURCE_ANSWER_CHANGED'); }
      else check(row.method==='operator_source_review');
      const node=records.observations?.[ref]??records.trends_evidence?.[ref]; check(node && at>=utc(node.value.observed_at??node.value.parsed?.metadata?.collected_at,fail) && at-utc(node.value.observed_at??node.value.parsed?.metadata?.collected_at,fail)<=7*DAY); matched[ref]=node.value;
    }
    const signals=await signalsFor(matched,proof.source_support,rows,at);
    check(deep(proof.source_signals,Object.fromEntries(SITES.map(s=>[s,pick(signals[s],['available','positive','used_ids',...(signals[s].evidence_support?['evidence_support']:[])])]))),'ADMIN_FUNCTIONAL_MARKET_SIGNALS_CHANGED');
    check(same(product.market_checks,SITES.map(s=>({source:s,state:signals[s].available?'Available':'Insufficient',detail:signals[s].reason,evidence_ids:signals[s].used_ids,url:null}))),'ADMIN_FUNCTIONAL_MARKET_DISPLAY_CHANGED');
    const available=SITES.filter(s=>signals[s].available).sort(), positive=available.filter(s=>signals[s].positive===true||signals[s].evidence_support==='reasonably_positive').sort();
    const basis={available_sources:available.length,positive_sources:positive.length,source_ids:available,positive_source_ids:positive,evidence_by_source:Object.fromEntries(available.map(s=>[s,signals[s].used_ids]))};
    const priority=available.length>=2?(product.kpi_matches>=6?1:2):positive.length?(product.kpi_matches>=6?3:4):null;
    check(product.opportunity_policy_id==='opportunity-priority-v2' && product.opportunity_priority===priority && same(product.opportunity_basis,basis) && product.market_label===(available.length?available.length+' sources with relevant information; no sales guarantee.':'No current usable relevant source information.'),'ADMIN_FUNCTIONAL_MARKET_PRIORITY_CHANGED');
    await witness(product,proof,records,rows,signals,at);
    const expectedRefs=Object.entries(proof.matches).sort(([a],[b])=>a<b?-1:a>b?1:0).map(([id,m])=>{ const o=records.observations?.[id]?.value,meta=records.trends_evidence?.[id]?.value?.parsed?.metadata; return {id,url:displayUrl(o?o.facts.url??null:meta.source_reference??null),source:o?o.source:'google-trends',observed_at:o?o.observed_at:meta.collected_at,relationship:m.relationship,relevance_reason:m.reason}; });
    for(const {row:r} of supplier.legacy) {const lead=r.input.id,pages=Object.fromEntries(r.input.pages.map(p=>[p.id,p])),refs=new Set([...r.result.relevance.references,...r.result.terms.flatMap(x=>x.references),...r.result.prices.flatMap(x=>x.references)]);for(const id of [...refs].sort())if(pages[id])expectedRefs.push({id:await key('supplier-ref',[lead,id]),url:displayUrl(pages[id].url),source:'Supplier page (reviewed)',observed_at:r.source_dates?.[id]??r.checked_at,date_basis:r.source_dates?.[id]?'supplier_page_capture':'supplier_interpretation_review',original_page_id:id});}
    expectedRefs.push(...supplier.source_refs);
    check(same(product.source_refs,expectedRefs),'ADMIN_FUNCTIONAL_SOURCE_DISPLAY_CHANGED');
    return {records,rows,signals,matched};
  }

  async function routeFacts(product, marketContext) {
    const proof=product.functional_route_proof,cid=product.concept_id,meaning=product.functional_definition.meaning_hash,at=utc(product.example_as_of,fail);
    check(keys(proof,['contract','concept_id','meaning_hash','as_of','records','record_order','run_scopes','proof_hash'])&&proof.contract==='functional-source-route-proof-v1'&&proof.concept_id===cid&&proof.meaning_hash===meaning&&proof.as_of===product.example_as_of&&proof.proof_hash===await digest(omit(proof,['proof_hash'])),'ADMIN_FUNCTIONAL_ROUTE_PROOF_INVALID');
    const records=await recordMaps(proof.records,product.source_revisions);
    check(routeProofTables.every(t=>Object.hasOwn(records,t))&&Object.keys(records).every(t=>memoryTables.includes(t)),'ADMIN_FUNCTIONAL_ROUTE_PROOF_INVALID');
    check(object(proof.record_order)&&same(Object.keys(proof.record_order).sort(),Object.keys(records).sort()),'ADMIN_FUNCTIONAL_ROUTE_PROOF_INVALID');
    for(const [table,ids] of Object.entries(proof.record_order)){check(Array.isArray(ids)&&ids.length===new Set(ids).size&&same([...ids].sort(),Object.keys(records[table]).sort()),'ADMIN_FUNCTIONAL_ROUTE_PROOF_INVALID');records[table]=Object.fromEntries(ids.map(id=>[id,records[table][id]]));}
    const state=Object.fromEntries(Object.entries(records).map(([table,rows])=>[table,Object.fromEntries(Object.entries(rows).map(([id,node])=>[id,node.value]))]));
    check(deep(state.concepts?.[cid]?.description,product.description),'ADMIN_FUNCTIONAL_ROUTE_PROOF_INVALID');
    const scope=await digest(product.description), checks=Object.fromEntries(Object.entries(state.source_checks).filter(([,r])=>!r.at||utc(r.at,fail)<=at));
    const tasks=Object.create(null), runs=[];
    for(const [id,saved] of Object.entries(state.query_tasks)) {
      if(saved.kind!=='all_source_task_v1'||utc(saved.prepared_at,fail)>at)continue;
      const task=structuredClone(saved), eligible=task.receipt_refs.filter(ref=>utc(state.source_checks[ref].at,fail)<=at);
      const futureReview=Object.values(state.source_checks).some(r=>r.kind==='all_source_relevance_v1'&&r.task_id===id&&utc(r.at,fail)>at);
      if(!deep(eligible,task.receipt_refs)||futureReview){Object.assign(task,{receipt_refs:eligible,target_results:{},attempt_refs:[],status:'prepared',reconcile_required:false});
        for(const ref of eligible){const e=state.source_checks[ref].envelope;Object.assign(task,{status:e.status,reason:e.reason,target_results:structuredClone(e.target_results)});if(e.dispatch_started===true)task.attempt_refs.push(e.acquisition_id);}
        for(const r of Object.values(checks))if(r.kind==='all_source_relevance_v1'&&r.task_id===id)for(const result of r.results)task.target_results[result.concept_id]=structuredClone(result);
        const dispatched=Object.values(checks).filter(r=>r.kind==='all_source_dispatch_v1'&&r.task_id===id), complete=new Set(eligible.map(ref=>checks[ref].envelope.acquisition_id));
        task.attempt_refs=[...new Set([...task.attempt_refs,...dispatched.map(r=>r.acquisition_id)])].sort();
        if(dispatched.some(r=>!complete.has(r.acquisition_id)))Object.assign(task,{status:'dispatching',reconcile_required:true,reason:'An actual check still needs its saved result.'});
      }
      if(task.dispatched_at&&utc(task.dispatched_at,fail)>at)delete task.dispatched_at;
      tasks[id]={...task,task_id:id};
    }
    const portableRuns={...proof.run_scopes};
    for(const [id,saved] of Object.entries(state.batch_runs??{}))if(saved.kind==='all_source_run_v1')portableRuns[id]={prepared_at:saved.prepared_at,task_ids:saved.task_ids,scope:{value:{route_pauses:saved.route_pauses??{},pending_preparation:saved.pending_preparation??[]}}};
    for(const [id,saved] of Object.entries(portableRuns)) {
      const view=Object.hasOwn(state.batch_runs??{},id)?saved.scope.value:(await original(saved.scope)).value;
      check(keys(saved,['prepared_at','task_ids','scope'])&&keys(view,['route_pauses','pending_preparation'])&&new Set(saved.task_ids).size===saved.task_ids.length&&saved.task_ids.every(id=>state.query_tasks[id]),'ADMIN_FUNCTIONAL_ROUTE_PROOF_INVALID');
      if(utc(saved.prepared_at,fail)>at)continue;
      runs.push({id,task_ids:saved.task_ids.filter(id=>tasks[id]),pending_preparation:view.pending_preparation,route_pauses:Object.fromEntries(Object.entries(view.route_pauses).filter(([,p])=>checks[p.receipt_id]))});
    }
    const definitions=Object.entries(state.source_checks).filter(([,r])=>r.kind==='functional-definition-v1'&&r.concept_id===cid);
    for(const [id] of definitions)await definition(records.source_checks[id],id);
    definitions.sort((a,b)=>a[1].revision-b[1].revision);
    for(const [index,[id,row]] of definitions.entries())check(row.revision===index+1&&row.previous_definition_id===(index?definitions[index-1][0]:null)&&(!index||row.previous_meaning_hash===definitions[index-1][1].meaning_hash),'ADMIN_FUNCTIONAL_ROUTE_MEANING_CHANGED');
    const latestDefinition=definitions.at(-1);
    check(latestDefinition?.[0]===product.functional_definition.definition_id&&utc(latestDefinition[1].reviewed_at,fail)<=at,'ADMIN_FUNCTIONAL_ROUTE_MEANING_CHANGED');
    const scopedRows=await routeScopedRows(records,product,at), identities=new Set(), candidates=new Set(), owners=new Map();
    const identity=o=>canonical([o.source,o.source_item,...(['blinkit','zepto','instamart'].includes(o.source)?[o.facts.location??null]:[])]);
    // Optional metadata fields are explicit null in the Python scope hash.
    const chartKey=o=>canonical(Object.fromEntries(['term','geo','time_range','property','category'].map(k=>[k,o.parsed?.metadata?.[k]??null])));
    for(const [tid,t] of Object.entries(tasks))if(t.source!=='supplier'&&Object.hasOwn(t.targets,cid))for(const rid of t.receipt_refs){const r=checks[rid],e=r?.envelope;
      check(r?.kind==='all_source_receipt_v1'&&r.envelope_hash===await hash(records.source_checks[rid].children.envelope.raw)&&r.task_id===tid&&r.route===t.route&&e.route===t.route&&r.request_hash===t.request_hash&&new Set([...t.attempt_refs,t.acquisition_id]).has(e.acquisition_id),'ADMIN_FUNCTIONAL_ROUTE_TASK_CHANGED');
      if(utc(e.observed_at,fail)>at)continue;
      for(const ref of [...e.observation_ids,...e.evidence_refs])if(state.observations[ref]||state.trends_evidence[ref]){candidates.add(ref);if(!owners.has(ref))owners.set(ref,new Set());owners.get(ref).add(tid);}
    }
    const semantics=new Set(Object.values(state.associations).filter(r=>r.concept_id===cid&&r.status==='resolved').map(r=>r.semantic_id));
    for(const [ref,o] of Object.entries(state.observations))if(semantics.has(o.semantic_id))candidates.add(ref);
    for(const [ref,o] of Object.entries(state.trends_evidence))if(o.concept_id===cid)candidates.add(ref);
    for(const ref of candidates)if(state.observations[ref])identities.add(identity(state.observations[ref]));
    const latest=new Map(),charts=new Map();
    for(const [ref,o] of Object.entries(state.observations)){const date=utc(o.observed_at,fail);if(date>at)continue;const k=identity(o),old=latest.get(k);if(!old||date>old.date)latest.set(k,{ref,date});}
    const chartKeys=new Set([...candidates].filter(ref=>state.trends_evidence[ref]).map(ref=>chartKey(state.trends_evidence[ref])));
    for(const [ref,o] of Object.entries(state.trends_evidence)){const date=o.parsed?.metadata?.collected_at;if(o.evidence_kind==='related_queries'||!date||utc(date,fail)>at)continue;const k=chartKey(o),old=charts.get(k),n=utc(date,fail);if(!old||n>old.date||n===old.date&&ref>old.ref)charts.set(k,{ref,date:n});}
    // The route capsule carries current global replacements and review heads.
    // An internally sound but superseded market capsule must not revive old
    // matches, or quietly omit a newer broad/unrelated correction.
    const currentIds=new Set([...latest.values()].filter(v=>at-v.date<=7*DAY).map(v=>v.ref)),marketCharts=new Map();
    for(const [ref,o] of Object.entries(state.trends_evidence)){const date=o.parsed?.metadata?.collected_at;if(o.evidence_kind==='related_queries'||!date)continue;const n=utc(date,fail);if(n>at||at-n>7*DAY)continue;const k=chartKey(o),old=marketCharts.get(k),usable=o.parsed.state==='Usable';if(!old||Number(usable)>Number(old.usable)||usable===old.usable&&(n>old.date||n===old.date&&ref>old.ref))marketCharts.set(k,{ref,date:n,usable});}
    for(const {ref} of marketCharts.values())currentIds.add(ref);
    const currentMatches=new Map(),rowMap=Object.fromEntries(scopedRows.map(r=>[r.id,r]));
    for(const row of scopedRows)if(['operator_source_review','functional_source_match_v4'].includes(row.method))for(const ref of row.references){if(!['observations','trends_evidence'].includes(ref.table)||!currentIds.has(ref.id)||source(row.route)==='supplier'||row.method!=='operator_source_review'&&!await matchAnswer(row,ref.id))continue;const old=currentMatches.get(ref.id);if(!old||utc(row.reviewed_at,fail)>utc(old.reviewed_at,fail)||utc(row.reviewed_at,fail)===utc(old.reviewed_at,fail)&&(row.revision>old.revision||row.revision===old.revision&&row.id>old.id))currentMatches.set(ref.id,row);}
    const acceptedMatches=Object.fromEntries([...currentMatches].filter(([,r])=>r.scope==='narrow_type'&&r.admissibility==='accepted'&&['exact_product','relevant_family'].includes(r.relationship)).map(([ref,r])=>[ref,{scope_review_id:r.id,scope_review_hash:r.record_hash,relationship:r.relationship,scope:r.scope,reason:r.reason,limitations:r.limitations}]));
    check(deep(acceptedMatches,product.functional_market_proof.matches),'ADMIN_FUNCTIONAL_MARKET_CURRENTNESS_CHANGED');
    const opinions=new Map(),orders=new Map();
    for(const row of scopedRows){if(row.method!=='functional_source_support_v4'||row.scope!=='narrow_type'||row.admissibility!=='accepted')continue;const p=row.job_proof,answer=p?.accepted_item,refs=row.references.filter(r=>['observations','trends_evidence'].includes(r.table)).map(r=>r.id).sort(),site=row.source==='trends'?'google-trends':row.source,judgment={positive:'reasonably_positive',neutral:'neutral',not_positive:'weak',insufficient:'unclear'}[row.support];
      if(p?.role!=='E'||p.prompt_version!=='research-e-source-support-v4'||!answer||p.accepted_item_hash!==await digest(answer)||!refs.length||!refs.every(ref=>Object.hasOwn(acceptedMatches,ref)&&currentIds.has(ref)))continue;
      const candidates=answer.source_support.filter(v=>v.source===site&&deep([...v.references].sort(),refs)&&v.judgment===judgment&&v.reason===row.reason),opinion=candidates[0];if(candidates.length!==1||!['observed','inferred','not_stated'].includes(opinion.basis)||['reasonably_positive','weak'].includes(opinion.judgment)&&!['observed','inferred'].includes(opinion.basis))continue;
      const date=Math.max(...refs.map(ref=>utc(state.observations[ref]?.observed_at??state.trends_evidence[ref]?.parsed.metadata.collected_at,fail))),order=[date,utc(row.reviewed_at,fail),row.id],old=orders.get(site);if(!old||order[0]>old[0]||order[0]===old[0]&&(order[1]>old[1]||order[1]===old[1]&&order[2]>old[2])){orders.set(site,order);opinions.set(site,{...opinion,review_id:row.id,reviewed_at:row.reviewed_at});}}
    const selected=new Set([...Object.values(acceptedMatches).map(r=>r.scope_review_id),...[...opinions.values()].map(r=>r.review_id)]),expectedScope=await digest({meaning_hash:meaning,heads:Object.fromEntries(scopedRows.map(r=>[r.binding_key,r.record_hash])),current_originals:Object.fromEntries([...currentMatches.keys()].sort().map(ref=>[ref,(records.observations[ref]??records.trends_evidence[ref]).hash]))});
    check(deep([...opinions.values()],product.functional_market_proof.source_support)&&same([...selected].sort(),Object.keys(marketContext.rows).sort())&&expectedScope===product.functional_market_proof.scope_hash&&[...selected].every(id=>rowMap[id].record_hash===marketContext.rows[id].record_hash),'ADMIN_FUNCTIONAL_MARKET_CURRENTNESS_CHANGED');
    const actual=[...[...latest].filter(([k])=>identities.has(k)).map(([,v])=>v.ref),...[...charts].filter(([k])=>chartKeys.has(k)).map(([,v])=>v.ref)].sort(), byRoute=new Map();
    const routeOf=o=>{const f=o.facts??{},capture=state.captures[o.capture_id],explicit=f.source_route||(object(capture?.reference)?capture.reference.route:null);return routeNames.includes(explicit)?explicit:context('observations','',o).route;};
    const supportedJobs=new Set(scopedRows.filter(r=>r.method==='functional_source_support_v4'&&r.job_proof).map(r=>canonical([r.job_proof.job_id,r.job_proof.accepted_item_hash]))), completed=new Set();
    for(const r of scopedRows)if(r.method==='functional_source_match_v4'&&supportedJobs.has(canonical([r.job_proof?.job_id,r.job_proof?.accepted_item_hash])))for(const ref of r.references)if(['observations','trends_evidence'].includes(ref.table)&&await matchAnswer(r,ref.id))completed.add(ref.id);
    for(const ref of actual){const o=state.observations[ref],chart=state.trends_evidence[ref];let route=chart?'google-trends':context('observations',ref,o).route;
      if(!route&&o){const c=state.captures[o.capture_id];route=c?context('captures',o.capture_id,c).route:null;if(!route){const owned=[...new Set([...(owners.get(ref)??[])].map(id=>tasks[id].route))];if(owned.length===1)route=owned[0];}}
      if(!route)continue;if(!byRoute.has(route))byRoute.set(route,[]);byRoute.get(route).push(ref);
    }
    const coverage=new Map();
    for(const [route,evidence] of byRoute){const pending=evidence.filter(ref=>!completed.has(ref)),matching=[];
      for(const row of scopedRows)if(row.method==='functional_source_match_v4'&&row.route===route){for(const ref of row.references)if(evidence.includes(ref.id)&&['observations','trends_evidence'].includes(ref.table)&&await matchAnswer(row,ref.id)){matching.push(row);break;}}
      const narrow=matching.some(r=>r.scope==='narrow_type'&&r.admissibility==='accepted'),status=narrow?'completed_relevant':pending.length?'interpretation_pending':matching.length&&matching.every(r=>r.relationship==='unrelated')?'completed_no_results':'insufficient';
      const dates=matching.flatMap(r=>r.source_contexts.map(c=>c.observed_at));if(!dates.length)dates.push(...evidence.map(ref=>state.observations[ref]?.observed_at??state.trends_evidence[ref].parsed.metadata.collected_at));
      coverage.set(route,{state:status,actual_attempt:true,at:dates.sort((a,b)=>utc(a,fail)-utc(b,fail)).at(-1),evidence_refs:[...new Set(evidence)].sort()});
    }
    const supplierScope=Object.fromEntries(['concept_id','label','category','function','mechanism','features'].map(k=>[k,k==='concept_id'?cid:product.description[k]??null]));
    for(const [id,r] of Object.entries(checks))if(r.kind==='supplier-effort-v1'&&r.concept_id===cid&&deep(r.sourcing_scope,supplierScope)){
      const route=supplierRouteAliases[r.route]??r.route;
      if(!supplierRoutes.includes(route))continue;await supplierEffortRecord(r);
      const status=({completed_empty:'completed_no_results',usable_leads:'completed_relevant',blocked:'blocked',failed:'failed',partial:'insufficient',interpretation_pending:'interpretation_pending',prelaunch_failed:'not_attempted'})[r.internal_outcome],old=coverage.get(route);
      if(!old||utc(r.at,fail)>utc(old.at,fail))coverage.set(route,{state:status,actual_attempt:r.effort_satisfied,at:r.at,evidence_refs:[id]});
    }
    const excluded=['excluded','merged','retired'].includes(state.concepts[cid].status)||product.brand_binding.verdict==='established',output=[];
    // A retired route has no client row, except in packets frozen before it retired.
    const legacyRows=Array.isArray(product.source_route_checks)&&product.source_route_checks.some(r=>retiredSupplierRoutes.includes(r?.route));
    for(const [route,label,role] of routeDefinitions){if(retiredSupplierRoutes.includes(route)&&!legacyRows)continue;let row=coverage.get(route)??{state:'not_attempted',actual_attempt:false,at:null,evidence_refs:[]};row={...row};
      const selected=[];for(const task of Object.values(tasks))if(task.route===route&&task.targets[cid]===scope&&!targetSuperseded(task,tasks,cid))selected.push(task);
      const taskIds=selected.map(t=>t.task_id),pauses=new Map();for(const run of runs)for(const [r,p] of Object.entries(run.route_pauses))for(const id of run.task_ids)if(tasks[id].route===r)pauses.set(id,p);
      if(excluded)row={state:'not_applicable',actual_attempt:false,at:null,evidence_refs:[]};
      else {for(const t of selected.sort((a,b)=>utc(a.dispatched_at??a.prepared_at,fail)-utc(b.dispatched_at??b.prepared_at,fail)||(a.task_id<b.task_id?-1:1))){const target=t.target_results[cid],physicalOnly=target&&['completed_no_results','blocked','failed'].includes(target.state)&&!t.receipt_refs.some(rid=>checks[rid]?.envelope?.observation_ids?.length),actualAttempt=!!t.attempt_refs.length;
          if(target&&target.description_hash===scope&&(role==='supplier'||physicalOnly)){if(row.state!=='completed_relevant'||target.state==='completed_relevant')row={...row,...target,actual_attempt:actualAttempt,at:t.receipt_refs.length?checks[t.receipt_refs.at(-1)].at:null};}
          else if(t.reconcile_required&&row.state!=='completed_relevant')Object.assign(row,{state:t.status==='dispatching'?'in_progress':t.status==='failed'?'failed':'blocked',actual_attempt:actualAttempt,at:t.dispatched_at??null});
          else if(pauses.has(t.task_id)&&row.state==='not_attempted')Object.assign(row,{state:'blocked',actual_attempt:false,blocked_by:pauses.get(t.task_id).receipt_id});
        }
        if(selected.some(t=>t.status==='acquired')&&row.state==='not_attempted')row.state='interpretation_pending';
      }
      const display=outcomeDisplay(row.state,row.actual_attempt),refs=new Set(row.evidence_refs.filter(ref=>['query_tasks','source_checks','observations','captures','trends_evidence'].some(table=>state[table]?.[ref]))),receiptIds=new Set(taskIds.flatMap(tid=>tasks[tid].receipt_refs.filter(rid=>utc(checks[rid].at,fail)<=at)));if(row.blocked_by)receiptIds.add(row.blocked_by);
      const metric=[],examples=Object.entries(marketContext.matched).filter(([,o])=>o.semantic&&routeOf(o)===route);let detail=display[1];
      const add=(name,value,ids)=>metric.push({name,value,evidence_ids:[...new Set(ids)].sort()});
      if(route==='meta-ads'){const ads=metaSample(marketContext.matched);if(ads.used.length){add('relevant_ads',ads.used.length,ads.used);for(const name of ['advertisers','active_ads','unknown_active_status','missing_advertiser_ids'])add(name,ads[name],ads.used);}}
      else if(examples.length){const unique=new Map();for(const [id,o] of examples)unique.set(canonical([o.source_item??id,...(['blinkit','zepto','instamart'].includes(route)?[o.facts.location??null]:[])]),[id,o]);const entries=[...unique.values()],used=entries.map(([id])=>id);add('relevant_listings',unique.size,used);add('displayed_prices',entries.filter(([,o])=>o.facts.price?.displayed!==undefined&&o.facts.price?.displayed!==null&&String(o.facts.price.displayed).trim()).length,used);
        if(['amazon-bestsellers','amazon-movers-and-shakers'].includes(route)){const ranked=entries.filter(([,o])=>verifiedAmazon(o)&&Number.isInteger(o.facts.position));if(ranked.length)add('list_rank_bounds',[Math.min(...ranked.map(([,o])=>o.facts.position)),Math.max(...ranked.map(([,o])=>o.facts.position))],ranked.map(([id])=>id));}
        if(['blinkit','zepto','instamart'].includes(route)){const values=entries.map(([,o])=>o.facts.available),known=values.some(v=>typeof v==='boolean');add('available_listings',known?values.filter(v=>v===true).length:null,used);add('unavailable_listings',known?values.filter(v=>v===false).length:null,used);add('unknown_availability',values.filter(v=>typeof v!=='boolean').length,used);const locations=[...new Set(entries.map(([,o])=>o.facts.location?.name).filter(Boolean).map(String))].sort(),unnamed=entries.some(([,o])=>!o.facts.location?.name);detail+=locations.length===1&&!unnamed?' Checked location: '+locations[0]+'.':locations.length?' Combined saved sample from: '+[...locations,...(unnamed?['location not stated']:[])].join('; ')+'.':' Checked locations were not stated.';}
      }
      if(route==='google-trends')for(const id of marketContext.signals['google-trends'].used_ids){const o=state.trends_evidence[id];if(!o)continue;add('chart_intervals',o.parsed.series.length,[id]);const s=trendsSignal(o.parsed,at);for(const name of ['nonzero_points','previous_mean_bounds','recent_mean_bounds'])if(Object.hasOwn(s,name))add(name,Array.isArray(s[name])?s[name].map(v=>String(v)):s[name],[id]);break;}
      for(const m of metric)for(const ref of m.evidence_ids)refs.add(ref);
      const dates=[...refs].filter(ref=>state.observations[ref]).map(ref=>state.observations[ref].observed_at);if(row.at)dates.push(row.at);
      if(metric.length)detail+=' '+metric.map(measurementText).join('; ')+'.';
      const latestReceipt=[...receiptIds].map(id=>[id,checks[id]]).sort((a,b)=>utc(a[1].at,fail)-utc(b[1].at,fail)||(a[0]<b[0]?-1:a[0]>b[0]?1:0)).at(-1);let last=null;
      if(latestReceipt?.[1].kind==='all_source_receipt_v1'){const [id,r]=latestReceipt,e=r.envelope,attempted=e.dispatch_started===true||taskIds.some(tid=>tasks[tid].attempt_refs.includes(e.acquisition_id));last={outcome:e.state,actual_attempt:attempted,at:r.at,receipt_id:id};if(row.state==='completed_relevant'&&['blocked','failed','unavailable'].includes(e.state))detail+=(attempted?' The most recent attempt '+(e.state==='blocked'?'was blocked':'failed'):' The latest check could not start')+'; earlier saved relevant results are retained.';}
      const urls=[...refs].sort().filter(ref=>state.observations[ref]).map(ref=>state.observations[ref].facts.url);
      output.push({route,source:label,source_role:role,description_hash:scope,outcome:row.state,state:display[0],actual_attempt:row.actual_attempt,detail,measurements:metric,evidence_ids:[...refs].sort(),task_ids:taskIds,receipt_ids:[...receiptIds].sort(),observed_at:dates.sort((a,b)=>utc(a,fail)-utc(b,fail)).at(-1)??null,url:displayUrl(urls.find(url=>typeof url==='string')??null),last_attempt:last,meaning_hash:meaning,scoped_evidence_ids:Object.keys(marketContext.rows).filter(id=>marketContext.rows[id].route===route).sort()});
    }
    check(same(output,product.source_route_checks),'ADMIN_FUNCTIONAL_ROUTE_DISPLAY_CHANGED');
  }

  async function matchAnswer(row,ref) {
    const p=row.job_proof,answer=p?.accepted_item;
    return row.method==='functional_source_match_v4'&&row.support==='insufficient'&&p?.role==='E'&&p.prompt_version==='research-e-source-support-v4'&&answer&&p.accepted_item_hash===await digest(answer)&&deep(answer.relevance?.filter(r=>r.id===ref),[{id:ref,scope:row.scope,relationship:row.relationship,reason:row.reason,limitations:row.limitations}]);
  }

  async function routeScopedRows(records,product,at) {
    const groups=new Map(),meaning=product.functional_definition.meaning_hash,cid=product.concept_id;
    for(const [id,node] of Object.entries(records.source_checks)) {
      const r=node.value;if(r.kind!=='scoped-evidence-v1')continue;
      check(r.record_hash===await rawHash(node,['record_hash'])&&id==='scoped-evidence-'+(await hash(rawObject(node,['reviewed_at','meaning_basis','record_hash','binding_key','revision','previous_binding_id']))).slice(0,40),'ADMIN_FUNCTIONAL_ROUTE_SCOPE_CHANGED');
      if(r.concept_id!==cid)continue;
      const binding={kind:r.kind,subject:r.subject,method:r.method,role:r.job_proof?.role??'operator',role_contract:r.job_proof?.prompt_version??'explicit-source-scope-v1',references:r.references,route:r.route,scope:r.scope,question:r.question,selected_fields:r.usable_fields.map(f=>pick(f,['reference','path','hash']))};
      check(r.binding_key===await key('scope-binding',binding)&&integer(r.revision,1),'ADMIN_FUNCTIONAL_ROUTE_SCOPE_CHANGED');
      if(!groups.has(r.binding_key))groups.set(r.binding_key,[]);groups.get(r.binding_key).push({id,node,row:r});
    }
    const output=[];
    for(const rows of groups.values()){
      rows.sort((a,b)=>a.row.revision-b.row.revision);let previous=null,previousTime=null,chosen=null;
      for(const [index,entry] of rows.entries()){const r=entry.row,date=utc(r.reviewed_at,fail);check(r.revision===index+1&&r.previous_binding_id===previous&&(previousTime===null||date>=previousTime),'ADMIN_FUNCTIONAL_ROUTE_SCOPE_CHANGED');previous=entry.id;previousTime=date;if(date<=at)chosen=entry;}
      if(!chosen||chosen.row.subject?.definition_hash!==meaning)continue;
      const {id,node,row:r}=chosen;
      let current=true;
      for(const ref of r.references)if(records[ref.table]?.[ref.id]?.hash!==ref.hash)current=false;
      const semanticIds=new Set(r.references.filter(ref=>ref.table==='observations').map(ref=>records.observations?.[ref.id]?.value.semantic_id));
      const associations=Object.fromEntries(Object.entries(records.associations).filter(([,n])=>semanticIds.has(n.value.semantic_id)).map(([id,n])=>[id,n.hash]));if(!deep(associations,r.association_hashes))current=false;
      for(const [tid,expected] of Object.entries(r.request_dependencies)){const t=records.query_tasks[tid]?.value;if(!t){current=false;continue;}const request=pick(t,['attempt_id','config_hash','request']);if(!deep({...request,hash:await digest(request)},expected))current=false;}
      if(r.question){const qnode=records.query_tasks[r.question.id],q=qnode?.value,b=q?.binding;if(!q||!await questionRecord(qnode)||!deep(q.subject,r.subject)||q.route!==r.route||!deep(r.question,{id:q.id,binding_hash:q.binding_hash,physical_request_fingerprint:q.physical_request_fingerprint}))current=false;else for(const dep of b.dependencies??[])if(records[dep.table]?.[dep.id]?.hash!==dep.hash)current=false;}
      const p=r.job_proof;
      if(p&&records.jobs?.[p.job_id]?.hash!==p.job_hash)current=false;
      if(!current)continue;
      await scoped(id,node,records,meaning,cid,at);
      for(const [ref,capsule] of Object.entries(p?.capture_contexts??{}))await metaContext(records,ref,capsule,node.children.job_proof.children.capture_contexts.children[ref]);
      output.push({...r,id});
    }
    return output.sort((a,b)=>a.id<b.id?-1:a.id>b.id?1:0);
  }

  async function questionRecord(node) {
    const q=node.value,b=q.binding;
    if(q.kind!=='research-question-v2'||!object(b)||b.contract!==q.kind||q.binding_hash!==await hash(node.children.binding.raw)||q.id!=='research-question-'+(await hash(node.children.binding.raw)).slice(0,40)||!['subject','route','purpose','physical_request_fingerprint'].every(k=>deep(q[k],b[k]))||!Array.isArray(q.wording_candidates))return false;
    const fingerprint=async n=>{const r=n.children.request,c=n.children.capture_options,m=n.value.method,route=q.route;
      if(!object(r?.value)||!object(c?.value)||!['GET','HEAD','POST'].includes(m)||!routeNames.includes(route))return null;
      const allowed=['schema_version','version','route','platform','provider_platform','transport','url','endpoint','urls','query','queries','term','department','node','nodes','country','language','locale','timezone','location','geo','geography','postcode','date','date_scope','date_from','date_to','timeframe','property','category','sort','filter','filters','pagination','page','pages','depth','limit','offset','cursor','options','body','headers'];
      if(!Object.keys(r.value).every(k=>allowed.includes(k))||r.value.route!==undefined&&r.value.route!==route||r.value.schema_version!==undefined&&r.value.schema_version!==1||r.value.version!==undefined&&r.value.version!=='quickcommerce-location-v1')return null;
      const raw='{"capture_options":'+c.raw+',"contract":"physical-request-v1","method":'+JSON.stringify(m)+',"request":'+rawObject(r,['schema_version','version','route'])+',"route":'+JSON.stringify(route)+'}';
      return await hash(raw);
    };
    if(q.physical_request_fingerprint!==await fingerprint(node))return false;
    const references=[];
    for(const n of Object.values(node.children.wording_candidates.children)){const w=n.value;if(!keys(w,['id','text','request','method','capture_options','physical_request_fingerprint'])||w.physical_request_fingerprint!==await fingerprint(n)||w.id!==await key('question-wording',[w.text,w.physical_request_fingerprint]))return false;references.push(pick(w,['id','text','physical_request_fingerprint']));}
    return deep(references,b.wording_candidates);
  }

  async function metaContext(records,ref,proof,node) {
    const c=proof.context,o=records.observations?.[ref]?.value,capture=records.captures?.[c?.capture?.id],fields=['kind','run_id','route','request','request_hash','prepared_at'];
    check(o?.source==='meta'&&capture&&c.capture.id===o.capture_id&&c.capture.hash===capture.hash&&c.observed_at===o.observed_at&&['source','acquisition_id','observed_at'].every(k=>deep(capture.value[k],o[k])),'ADMIN_FUNCTIONAL_META_REQUEST_CHANGED');
    const savedRequest=capture.value.request??(object(capture.value.reference)?capture.value.reference.request??null:null);
    if(c.request_basis==='saved_capture_request')check(c.state==='verified'&&deep(c.request,savedRequest)&&c.original_task===null&&c.original_receipt===null&&proof.original_task_binding===null&&proof.original_task_snapshot===null,'ADMIN_FUNCTIONAL_META_REQUEST_CHANGED');
    else if(c.request_basis==='not_saved')check(c.state==='not_saved'&&c.request===null&&savedRequest===null&&c.original_task===null&&c.original_receipt===null&&proof.original_task_binding===null&&proof.original_task_snapshot===null,'ADMIN_FUNCTIONAL_META_REQUEST_CHANGED');
    else {
      const t=records.query_tasks?.[c.original_task?.id]?.value,receipt=records.source_checks?.[c.original_receipt?.id],e=receipt?.value?.envelope;
      check(c.request_basis==='receipt_bound_task_request'&&c.state==='verified'&&t?.kind==='all_source_task_v1'&&t.route==='meta-ads'&&await key('source-task',[t.run_id,'meta-ads',t.request])===c.original_task.id&&t.request_hash===await digest(t.request)&&t.receipt_refs.includes(c.original_receipt.id)&&receipt?.hash===c.original_receipt.hash&&receipt.value.kind==='all_source_receipt_v1'&&receipt.value.task_id===c.original_task.id&&receipt.value.route==='meta-ads'&&e?.route==='meta-ads'&&receipt.value.request_hash===t.request_hash&&receipt.value.envelope_hash===await hash(receipt.children.envelope.raw)&&c.original_receipt.id===await key('source-receipt',[c.original_task.id,o.acquisition_id,receipt.value.envelope_hash])&&e.acquisition_id===o.acquisition_id&&new Set([...t.attempt_refs,t.acquisition_id]).has(o.acquisition_id)&&e.dispatch_started===true&&e.observed_at===o.observed_at&&e.observation_ids.includes(ref)&&e.capture_ids.includes(o.capture_id)&&deep(t.request,c.request)&&deep(pick(t,fields),proof.original_task_binding)&&await hash(node.children.original_task_snapshot.raw)===c.original_task.hash&&deep(pick(proof.original_task_snapshot,fields),proof.original_task_binding),'ADMIN_FUNCTIONAL_META_REQUEST_CHANGED');
    }
  }

  function targetSuperseded(task,tasks,cid){const marker=task.superseded_targets?.[cid];if(!marker)return false;const replacement=tasks[marker.task_id];check(replacement?.kind==='all_source_task_v1'&&replacement.run_id===task.run_id&&replacement.route===task.route&&task.request.department==='home'&&replacement.request.department==='kitchen/1380374031'&&marker.request_hash===replacement.request_hash&&replacement.targets[cid]===task.targets[cid]&&!task.reconcile_required,'ADMIN_FUNCTIONAL_ROUTE_TASK_CHANGED');return true;}
  function verifiedAmazon(o){return o.source==='amazon'&&!o.facts.limitations?.length&&['bestsellers','movers-and-shakers'].includes(o.facts.list_type)&&(!Object.hasOwn(o.facts,'direct_capture')||(o.facts.scope_verified===true&&integer(o.facts.position,1)&&object(o.facts.direct_capture)));}
  function metaSample(matched){const ads=new Map(),conflicts=new Set();for(const [id,o] of Object.entries(matched))if(o.source==='meta'){const f=o.facts;let advertiser=f.advertiser_id??null;if(advertiser===null&&typeof f.advertiser_url==='string'){try{const u=new URL(f.advertiser_url);if(u.protocol==='https:'&&['facebook.com','www.facebook.com'].includes(u.hostname)&&!u.username&&!u.password&&u.pathname.replaceAll('/','')&&!/^\/(ads\/|login|help|privacy|policies)/.test(u.pathname))advertiser='page-url:https://www.facebook.com'+u.pathname.replace(/\/$/,'').toLowerCase()+u.search;}catch{}}const row={id,advertiser,active:f.active??null,started:f.started_at??null};if(ads.has(f.ad_id)&&!deep(pick(ads.get(f.ad_id),['advertiser','active','started']),pick(row,['advertiser','active','started'])))conflicts.add(f.ad_id);ads.set(f.ad_id,row);}const rows=[...ads].filter(([id])=>!conflicts.has(id)).map(([,r])=>r),advertisers=new Set(rows.map(r=>r.advertiser).filter(v=>v!==null));return {used:rows.map(r=>r.id).sort(),advertisers:advertisers.size||null,active_ads:rows.some(r=>r.active!==null)?rows.filter(r=>r.active===true).length:null,unknown_active_status:rows.filter(r=>r.active===null).length,missing_advertiser_ids:rows.filter(r=>r.advertiser===null).length};}
  async function supplierEffortRecord(r){
    check(r.kind==='supplier-effort-v1'&&r.schema_version===2&&r.source==='supplier'&&r.sourcing_scope_hash===await digest(r.sourcing_scope)&&Array.isArray(r.receipt_refs)&&r.receipt_refs.length<=10,'ADMIN_FUNCTIONAL_SUPPLIER_EFFORT_CHANGED');
    const actual=[],dates=[],seen=new Set();
    for(const ref of r.receipt_refs){check(ID.test(ref.id)&&HASH.test(ref.hash)&&ref.reference===r.reference&&!seen.has(ref.id),'ADMIN_FUNCTIONAL_SUPPLIER_EFFORT_CHANGED');seen.add(ref.id);utc(ref.checked_at,fail);publicUrl(ref.requested_url);if(ref.final_url!==null)publicUrl(ref.final_url);
      if(ref.proof==='none')check(['unknown','not_dispatched'].includes(ref.execution_stage)&&ref.attempt_started_at===null&&ref.execution_ref===null,'ADMIN_FUNCTIONAL_SUPPLIER_EFFORT_CHANGED');
      else {check(['owned_reader_execution','legacy_http_response'].includes(ref.proof)&&['attempt_started','response_received'].includes(ref.execution_stage)&&utc(ref.attempt_started_at,fail)<=utc(ref.checked_at,fail),'ADMIN_FUNCTIONAL_SUPPLIER_EFFORT_CHANGED');if(ref.proof==='legacy_http_response')check(ref.execution_stage==='response_received'&&ref.execution_ref===null&&ref.final_url!==null&&integer(ref.http_status,100,599),'ADMIN_FUNCTIONAL_SUPPLIER_EFFORT_CHANGED');else check(/^(?:public-fetch|supplier-browser)-[a-f0-9]{32}$/.test(ref.execution_ref??'')&&(ref.execution_stage!=='response_received'||integer(ref.http_status,100,599)),'ADMIN_FUNCTIONAL_SUPPLIER_EFFORT_CHANGED');actual.push(ref.id);dates.push(ref.attempt_started_at);}
    }
    check(same(r.actual_attempt_refs,actual.sort())&&r.effort_satisfied===Boolean(actual.length)&&r.last_attempt_at===(dates.sort((a,b)=>utc(a,fail)-utc(b,fail)).at(-1)??null)&&(!actual.length?r.internal_outcome==='prelaunch_failed':true)&&(!r.accepted_lead_refs.length||actual.length&&r.internal_outcome==='usable_leads'),'ADMIN_FUNCTIONAL_SUPPLIER_EFFORT_CHANGED');
  }

  async function signalsFor(matched,support,rows,at) {
    const signals=Object.fromEntries(SITES.map(s=>[s,{available:false,positive:null,used_ids:[],reason:'No fresh reviewed relevant source information.'}]));
    const observations=Object.entries(matched).filter(([,o])=>o.semantic), ads=new Map(), conflicts=new Set();
    for(const [id,o] of observations.filter(([,o])=>o.source==='meta')) {const f=o.facts;let advertiser=f.advertiser_id;if(advertiser===null&&typeof f.advertiser_url==='string'){try{const u=new URL(f.advertiser_url);if(u.protocol==='https:'&&['facebook.com','www.facebook.com'].includes(u.hostname)&&!u.username&&!u.password&&u.pathname.replaceAll('/','')&&!/^\/(ads\/|login|help|privacy|policies)/.test(u.pathname))advertiser='page-url:https://www.facebook.com'+u.pathname.replace(/\/$/,'').toLowerCase()+u.search;}catch{}}const v={advertiser,active:f.active,started:f.started_at,id,observed:o.observed_at};if(ads.has(f.ad_id)&&!same(pick(ads.get(f.ad_id),['advertiser','active','started']),pick(v,['advertiser','active','started'])))conflicts.add(f.ad_id);ads.set(f.ad_id,v);}
    const valid=[...ads.entries()].filter(([id])=>!conflicts.has(id)).map(([,v])=>v), advertisers=new Set(valid.map(v=>v.advertiser).filter(v=>v!==null)),active=valid.filter(v=>v.active===true);
    const positive=advertisers.size>=3||active.length>=5&&active.some(v=>v.started!==null&&utc(v.observed,fail)-utc(v.started,fail)>=14*DAY);
    signals.meta={available:!!valid.length,positive:positive?true:valid.length?false:null,used_ids:valid.map(v=>v.id).sort(),reason:'Relevant retained ads; missing ad metadata is not negative demand proof.'};
    const verified=o=>o.source==='amazon'&&!o.facts.limitations?.length&&['bestsellers','movers-and-shakers'].includes(o.facts.list_type)&&(!Object.hasOwn(o.facts,'direct_capture')||(o.facts.scope_verified===true&&integer(o.facts.position,1)&&object(o.facts.direct_capture)));
    for(const site of ['amazon','flipkart','blinkit','zepto','instamart']) {const selected=observations.filter(([,o])=>o.source===site&&(verified(o)||['amazon','flipkart'].includes(site)&&o.facts.list_type==='product_search'||['blinkit','zepto','instamart'].includes(site)&&o.facts.list_type==='quickcommerce_context'));const good=selected.some(([,o])=>verified(o)); signals[site]={available:!!selected.length,positive:good?true:selected.length?false:null,used_ids:selected.map(([id])=>id).sort(),reason:good?'Relevant reviewed India list presence; all Amazon routes count as one source.':selected.length?'Relevant current listings supply product, price or availability context; not sales proof.':'No fresh reviewed relevant source information.'};}
    const charts=Object.entries(matched).filter(([,o])=>o.parsed);
    if(charts.length){const usable=charts.filter(([,o])=>o.parsed.state==='Usable'),[id,o]=(usable.length?usable:charts).sort((a,b)=>utc(a[1].parsed.metadata.collected_at,fail)-utc(b[1].parsed.metadata.collected_at,fail)||a[0].localeCompare(b[0])).at(-1);const signal=trendsSignal(o.parsed,at);signals['google-trends']={available:signal.available,positive:signal.available?signal.positive:null,used_ids:signal.available?[id]:[],reason:signal.reason,...(signal.evidence_support?{evidence_support:signal.evidence_support}:{})};}
    for(const opinion of support) {
      const row=rows[opinion.review_id],p=row?.job_proof,answer=p?.accepted_item;
      const originalRefs=row?.references?.filter(r=>['observations','trends_evidence'].includes(r.table)).map(r=>r.id).sort();
      const judgments={positive:'reasonably_positive',neutral:'neutral',not_positive:'weak',insufficient:'unclear'};
      const candidates=answer?.source_support?.filter(v=>v.source===opinion.source&&same([...v.references].sort(),originalRefs)&&v.judgment===judgments[row.support]&&v.reason===row.reason);
      const judged=candidates?.[0];
      check(row?.method==='functional_source_support_v4'&&row.scope==='narrow_type'&&row.admissibility==='accepted'&&p.role==='E'&&p.prompt_version==='research-e-source-support-v4'&&p.accepted_item_hash===await digest(answer)&&candidates?.length===1&&['observed','inferred','not_stated'].includes(judged.basis)&&(!['reasonably_positive','weak'].includes(judged.judgment)||(originalRefs.length&&['observed','inferred'].includes(judged.basis)))&&same(opinion,{...judged,review_id:opinion.review_id,reviewed_at:row.reviewed_at}),'ADMIN_FUNCTIONAL_SOURCE_SUPPORT_CHANGED');
      const signal=signals[opinion.source];check(signal?.available&&opinion.references.length&&opinion.references.every(r=>signal.used_ids.includes(r)));
      if(opinion.judgment==='reasonably_positive'&&(opinion.source!=='google-trends'||opinion.references.some(r=>matched[r]?.parsed?.series?.some(p=>p.state==='bounded_nonzero'||p.state==='observed'&&p.value>0))))signal.evidence_support='reasonably_positive';
    }
    return signals;
  }

  function trendsSignal(parsed, at) {
    const meta=parsed.metadata??{}, series=parsed.series;
    const base={available:false,positive:null,reason:'No usable verified India twelve-month series.'};
    if(parsed.state!=='Usable')return {...base,reason:parsed.reason??base.reason};
    if(meta.geo!=='IN'||meta.time_range!=='today 12-m'||meta.property!=='web'||meta.scope_verified!==true)return base;
    const captured=utc(meta.collected_at,fail);if(at<captured||at-captured>7*DAY)return {...base,reason:'Capture is outside its seven-day evidence window.'};
    const invalid={...base,reason:'Series contract is invalid; no movement score calculated.'};
    if(!Array.isArray(series)||series.length>1000)return invalid;
    const date=value=>{if(typeof value!=='string'||!/^\d{4}-\d\d-\d\d$/.test(value))return NaN;const n=Date.parse(value+'T00:00:00Z');return Number.isFinite(n)&&new Date(n).toISOString().slice(0,10)===value?n:NaN;};
    let previousStart=null,previousEnd=null,contextGood=series.length>=8;const complete=[];
    for(const p of series){const start=date(p.start),end=date(p.end);if(!Number.isFinite(start)||!Number.isFinite(end)||start>end||previousStart!==null&&start<=previousStart||![null,true,false].includes(p.partial??null))return invalid;previousStart=start;
      if(!(p.state==='observed'&&integer(p.value,0,100)&&p.lower===p.value&&p.upper===p.value||p.state==='bounded_nonzero'&&p.value===null&&p.lower===0&&p.upper===1&&p.upper_exclusive===true||p.state==='missing'&&p.value===null&&p.lower===null&&p.upper===null))return invalid;
      if(p.state==='missing'||!['weekly','monthly'].includes(p.interval)||previousEnd!==null&&start!==previousEnd+DAY)contextGood=false;previousEnd=end;
      if(p.partial!==true&&end<Math.floor(captured/DAY)*DAY)complete.push(p);
    }
    if(complete.length<8)return {...base,reason:'Fewer than eight completed comparable intervals.'};
    const duration=(date(complete.at(-1).end)-date(complete[0].start))/DAY,age=(Math.floor(captured/DAY)*DAY-date(complete.at(-1).end))/DAY;
    if(duration<300||duration>400)return {...base,reason:'Available series does not cover the requested twelve-month period.'};
    if(!['weekly','monthly'].includes(complete.at(-1).interval)||new Set(complete.map(p=>p.interval)).size!==1)return {...base,reason:'Intervals are not comparable.'};
    if(age>(complete.at(-1).interval==='weekly'?7:32))return {...base,reason:'Latest completed chart interval is not current at capture.'};
    base.available=contextGood;const windows=complete.slice(-8);
    for(let i=1;i<windows.length;i++)if(date(windows[i-1].end)+DAY!==date(windows[i].start))return {...base,reason:'Comparison windows contain an interval gap.'};
    if(windows.some(p=>p.lower===null||p.upper===null))return {...base,reason:'Comparison windows contain missing interest values.'};
    const nonzero=complete.filter(p=>p.state==='bounded_nonzero'||p.value!==null&&p.value>0).length;
    const result={...base,nonzero_points:nonzero};
    if(nonzero<4)return {...result,reason:'Series has fewer than four nonzero points; zero is not proof of absent demand.'};
    const old=windows.slice(0,4),recent=windows.slice(4),mean=(rows,field)=>rows.reduce((a,p)=>a+p[field],0)/4;
    const lowOld=mean(old,'lower'),highOld=mean(old,'upper'),lowNew=mean(recent,'lower'),highNew=mean(recent,'upper');
    Object.assign(result,{previous_mean_bounds:[lowOld,highOld],recent_mean_bounds:[lowNew,highNew]});
    const related=(parsed.rising_related_queries??[]).some(q=>object(q)&&q.rising===true&&q.same_product===true&&text(q.query)&&text(q.reference));
    if(highOld===0)return recent.filter(p=>p.state==='bounded_nonzero'||p.value>0).length>=2&&related?{...result,positive:true,reason:'Zero previous mean; at least two recent nonzero periods and a same-product rising query.'}:{...result,reason:'Zero previous mean needs two recent nonzero periods and supporting rising-query evidence.'};
    if(related)return {...result,positive:true,reason:'Supported same-product rising related query satisfies the alternative rule.'};
    if(lowOld===0)return {...result,reason:'Bounded values leave the previous mean potentially zero; exact movement cannot be established.'};
    if(lowNew/highOld>=1.15&&lowNew-highOld>=5)return {...result,positive:true,reason:'Recent mean is at least 15 percent and five points above the preceding mean.'};
    if(highNew/lowOld<1.15||highNew-lowOld<5)return {...result,positive:false,reason:'Comparable relative interest does not meet the rising rule; this is not a sales or demand estimate.'};
    return {...result,reason:"The rising decision depends on the unknown exact value of a bounded '<1' point."};
  }

  async function witness(product,proof,records,rows,signals,at) {
    const w=product.priority_support,need=[1,2].includes(product.opportunity_priority)?2:1;
    check(keys(w,['contract','meaning_hash','priority','sources','valid_until'])&&w.contract==='functional-priority-support-v1'&&w.meaning_hash===proof.meaning_hash&&w.priority===product.opportunity_priority&&Array.isArray(w.sources)&&w.sources.length===need);
    const seen=new Set(),refs=new Set(),all=[];
    for(const s of w.sources){check(keys(s,['source','kind','references','scope_reviews','valid_until'])&&signals[s.source]?.available&&!seen.has(s.source)&&s.kind===(need===2?'available':'positive')&&Array.isArray(s.references)&&s.references.length&&Array.isArray(s.scope_reviews)&&s.scope_reviews.length);seen.add(s.source);if(need===1)check(signals[s.source].positive===true||signals[s.source].evidence_support==='reasonably_positive');const dates=[];for(const r of s.references){const n=records[r.table]?.[r.id],date=n?.value?.observed_at??n?.value?.parsed?.metadata?.collected_at;check(keys(r,['table','id','hash','source','observed_at'])&&['observations','trends_evidence'].includes(r.table)&&n?.hash===r.hash&&r.observed_at===date&&r.source===s.source&&signals[s.source].used_ids.includes(r.id)&&!refs.has(r.id));refs.add(r.id);all.push(r);dates.push(utc(date,fail));}check(utc(s.valid_until,fail)===Math.min(...dates)+7*DAY);for(const r of s.scope_reviews)check(rows[r.id]?.record_hash===r.hash&&rows[r.id].references.some(v=>s.references.some(x=>v.id===x.id)));}
    check(ordered(w.sources.map(s=>s.source))&&utc(w.valid_until,fail)===Math.min(...w.sources.map(s=>utc(s.valid_until,fail)))&&product.support_valid_until===w.valid_until&&same(product.priority_support_refs,[...refs].sort()),'ADMIN_FUNCTIONAL_WITNESS_CHANGED');
    const release=asOf===null?at:utc(asOf,fail);check(release<=utc(w.valid_until,fail)&&all.every(r=>utc(r.observed_at,fail)<=release),'ADMIN_FUNCTIONAL_WITNESS_STALE');
    // Rebuild the complete minimal witness. An aggregate model opinion cannot
    // be cherry-picked into a newer, one-row favourable citation.
    const verified=o=>o.source==='amazon'&&!o.facts.limitations?.length&&['bestsellers','movers-and-shakers'].includes(o.facts.list_type)&&(!Object.hasOwn(o.facts,'direct_capture')||(o.facts.scope_verified===true&&integer(o.facts.position,1)&&object(o.facts.direct_capture)));
    const iso=(value, originalDate)=>{const prefix=new Date(value).toISOString().slice(0,19),fraction=/\.(\d{1,6})(?:Z|\+00:00)$/.exec(originalDate)?.[1];return prefix+(fraction&&Number(fraction)?'.'+fraction.padEnd(6,'0'):'')+'+00:00';};
    const choices=[];
    for(const site of SITES) {
      const signal=signals[site];if(!signal.available)continue;
      const groups=need===2?signal.used_ids.map(r=>[r]):[];
      if(need===1) {
        if(signal.positive===true) {
          if(['amazon','flipkart'].includes(site))groups.push(...signal.used_ids.filter(r=>records.observations?.[r]&&verified(records.observations[r].value)).map(r=>[r]));
          else groups.push(signal.used_ids);
        }
        if(signal.evidence_support==='reasonably_positive')groups.push(...proof.source_support.filter(s=>s.source===site&&s.judgment==='reasonably_positive').map(s=>s.references));
      }
      const candidates=[];
      for(const group of groups) {
        const originalRefs=[],reviewIds=new Set();
        for(const ref of [...new Set(group)].sort()) {
          const table=records.observations?.[ref]?'observations':'trends_evidence',n=records[table]?.[ref], date=n?.value.observed_at??n?.value.parsed?.metadata?.collected_at;
          check(n&&proof.matches[ref]); originalRefs.push({table,id:ref,hash:n.hash,source:site,observed_at:date});reviewIds.add(proof.matches[ref].scope_review_id);
        }
        for(const support of proof.source_support)if(need===1&&support.source===site&&support.judgment==='reasonably_positive'&&same([...support.references].sort(),[...new Set(group)].sort()))reviewIds.add(support.review_id);
        const oldest=originalRefs.reduce((a,b)=>utc(a.observed_at,fail)<=utc(b.observed_at,fail)?a:b), expiry=utc(oldest.observed_at,fail)+7*DAY;
        const row={source:site,kind:need===2?'available':'positive',references:originalRefs,scope_reviews:[...reviewIds].sort().map(id=>({id,hash:rows[id].record_hash})),valid_until:iso(expiry,oldest.observed_at)};
        candidates.push({row,expiry,tie:await digest(row)});
      }
      if(candidates.length) choices.push(candidates.sort((a,b)=>a.expiry-b.expiry||(a.tie<b.tie?-1:a.tie>b.tie?1:0)).at(-1));
    }
    const groups=need===1?choices.map(v=>[v]):choices.flatMap((v,i)=>choices.slice(i+1).map(other=>[v,other]));
    check(groups.length>0);groups.sort((a,b)=>Math.min(...a.map(v=>v.expiry))-Math.min(...b.map(v=>v.expiry))||canonical(a.map(v=>v.row.source).sort()).localeCompare(canonical(b.map(v=>v.row.source).sort())));
    const chosen=groups.at(-1).map(v=>v.row).sort((a,b)=>a.source<b.source?-1:1), min=chosen.reduce((a,b)=>utc(a.valid_until,fail)<=utc(b.valid_until,fail)?a:b);
    check(same(w,{contract:'functional-priority-support-v1',meaning_hash:proof.meaning_hash,priority:product.opportunity_priority,sources:chosen,valid_until:min.valid_until}),'ADMIN_FUNCTIONAL_WITNESS_CHANGED');
  }

  async function assets() {
    const receipt=facts.report_asset_receipt,plan=receipt?.plan;
    check(keys(receipt,['contract','issue_id','revision','plan_hash','plan','record_hash'])&&receipt.contract==='report-owned-assets-v1'&&receipt.issue_id===facts.issue_id&&receipt.revision===facts.revision&&receipt.record_hash===await digest(omit(receipt,['record_hash']))&&receipt.plan_hash===await digest(plan)&&manifest.asset_receipt_hash===await digest(receipt),'ADMIN_FUNCTIONAL_ASSET_RECEIPT_CHANGED');
    check(keys(plan,['contract','transform_contract','encoded_budget','embedded_bytes','entries'])&&plan.contract==='report-assets-plan-v1'&&plan.transform_contract==='report-display-image-v1'&&integer(plan.encoded_budget,0,8*1024*1024)&&Array.isArray(plan.entries)&&plan.entries.length<=21);
    const ids=new Set(),photos=new Map();let encoded=0,logos=0;
    for(const row of plan.entries){const s=row.source;check(keys(row,['asset_id','source','display','transform','state','reason','failure_code'])&&['product_photo','brand_logo'].includes(s.kind)&&ID.test(s.owner_id)&&row.asset_id===await key('report-asset',[s.kind,s.owner_id])&&!ids.has(row.asset_id));ids.add(row.asset_id);if(s.kind==='product_photo'){check(!photos.has(s.owner_id));photos.set(s.owner_id,row);}else{logos++;check(s.owner_id==='brand-logo'&&logos<=1);}if(row.state==='omitted'){check(row.display===null&&row.transform===null&&text(row.reason)&&row.reason.length<=300&&/^[A-Z][A-Z0-9_]{1,199}$/.test(row.failure_code));continue;}const d=row.display,t=row.transform;check(row.state==='ready'&&keys(d,['sha256','format','width','height','bytes'])&&HASH.test(s.sha256)&&HASH.test(d.sha256)&&['JPEG','PNG','WEBP'].includes(d.format)&&integer(d.width,1,1600)&&integer(d.height,1,1600)&&integer(d.bytes,1,256*1024)&&row.reason===null&&row.failure_code===null);check(keys(t,['contract','pillow','format','long_edge','quality','transparent','orientation_corrected','full_frame','upscaled'])&&t.contract==='report-display-image-v1'&&text(t.pillow)&&t.pillow.length<=32&&t.format===d.format&&t.orientation_corrected===true&&t.full_frame===true&&t.upscaled===false&&[[1600,85],[1200,80],[960,75]].some(([e,q])=>e===t.long_edge&&(d.format==='PNG'?t.quality===null:t.quality===q))&&Math.max(d.width,d.height)<=t.long_edge&&t.transparent===(d.format!=='JPEG'));encoded+=4*Math.ceil(d.bytes/3)+({JPEG:'data:image/jpeg;base64,',PNG:'data:image/png;base64,',WEBP:'data:image/webp;base64,'})[d.format].length;}
    check(encoded===plan.embedded_bytes&&encoded<=plan.encoded_budget&&same([...photos.keys()].sort(),facts.products.map(p=>p.concept_id).sort()));
    const receiptHash=await digest(receipt);
    for(const p of facts.products){const row=photos.get(p.concept_id);if(row.state==='omitted'){check(p.display_asset===null);continue;}const d=row.display,s=row.source,ext={JPEG:'.jpg',PNG:'.png',WEBP:'.webp'}[d.format];check(same(p.display_asset,{asset_id:row.asset_id,owned_relative_ref:'assets/'+d.sha256+ext,output_sha256:d.sha256,bytes:d.bytes,source_hash:s.sha256,transform_contract:'report-display-image-v1',asset_receipt_hash:receiptHash}));check(p.image_provenance?.source_kind==='public_product_image'&&p.image_provenance.owner_id===p.concept_id&&['id','source_ref','sha256','source_url','observed_at','relationship'].every(k=>deep(p.image_provenance[k],s[k])),'ADMIN_FUNCTIONAL_ASSET_SOURCE_CHANGED');}
  }

  async function writing(language) {
    const narrative=facts.narratives[language], proof=narrative?.writing_proof;
    check(keys(proof,['contract','items'])&&proof.contract==='report-writing-acceptance-v1'&&object(proof.items),'ADMIN_FUNCTIONAL_WRITING_PROOF_INVALID');
    const titleId=await key('report-title',[facts.issue_id,language,'writing-input-v1']), hindi=language==='hi-Latn';
    const context={issue_id:facts.issue_id,language,allowed_placeholders:['title','rank','kpi_matches','date','retail_price','supplier_price'].map(p=>'{{'+p+'}}'),fact_policy:'Supplied facts only; numerical displays, market labels, links and commercial terms remain code-owned.',fit_policy_id:'fit-10-v1',criterion_ids:FIT_IDS,opportunity_policy_id:'opportunity-priority-v2',source_priority_rule:'Distinct confirmed platforms count once. Priority and source counts are code-owned. Catalogue presence and prices are context, not sales proof. Two usable platforms do not require growth; one platform needs supported positive context.',recommendation_contract:'functional-type-examples-v1',writing_contract:'writing-input-v1'};
    const english=hindi?facts.narratives.en:null;
    check(!hindi||object(english?.writing_proof?.items),'ADMIN_FUNCTIONAL_WRITING_ENGLISH_REQUIRED');
    const items=[{id:titleId,kind:'report_title',issue_id:facts.issue_id,language,facts:[],required_limitation:'',update_fact:'',source_english:english?{title:english.title}:null,criteria:[]}];
    const labels={meta:'Facebook ads',amazon:'Amazon India',flipkart:'Flipkart','google-trends':'Google Trends',blinkit:'Blinkit',zepto:'Zepto',instamart:'Instamart'};
    for(const p of facts.products) {
      const f=[{id:'title',text:p.title,basis:'observed',references:[]},{id:'market-label',text:p.market_label,basis:'observed',references:[]}];
      f.push(...p.market_checks.map((c,i)=>({id:'market-'+i,text:c.source+': '+c.state+'. '+c.detail,basis:c.interpretation_only&&c.basis!=='observed'?'inferred':'observed',references:[]})));
      f.push({id:'confirmed-sources',text:p.opportunity_basis.source_ids.map(s=>labels[s]).join(', '),basis:'observed',references:Object.values(p.opportunity_basis.evidence_by_source).flat().sort()});
      f.push(...p.writing_facts.map((r,i)=>({text:r.text,basis:r.basis,references:r.references,id:'benefit-'+i})));
      f.push(...p.criteria.map(r=>({id:'criterion-'+r.id,text:r.state+': '+r.reason,basis:r.basis,references:r.references})));
      const update=p.status==='Updated'?p.update_reason??'':''; if(update)f.push({id:'update',text:update,basis:'observed',references:[]});
      const clauses=[];
      if(!p.suppliers.length)clauses.push(hindi?'Supplier search se koi kaam ka result nahi mila.':'Supplier search did not produce any usable results.');
      else {const groups=p.suppliers.filter(s=>s.scoped_supplier_contract==='supplier-scoped-terms-v2'),legacy=p.suppliers.filter(s=>s.scoped_supplier_contract!=='supplier-scoped-terms-v2');
        if(groups.length){clauses.push(hindi?'Company ki jankari aur har offer ki kharid aur dispatch ki shartein alag dikhayi gayi hain.':"Company details and each offer's purchase and dispatch terms are shown separately.");const offers=groups.flatMap(s=>s.product_offers);
          if(!offers.length)clauses.push(hindi?'Supplier ka lead is product type ke offer ka confirmation nahi hai.':'A supplier lead is not a confirmed offer for this product type.');
          else if(offers.some(o=>!['single_piece_purchase','direct_customer_dispatch'].every(f=>o.effective_terms.some(t=>t.field===f&&t.value==='Yes'&&t.qualified))))clauses.push(hindi?'Har offer ke liye single-piece kharid aur dispatch confirm nahi hai.':'Single-piece purchase and dispatch are not confirmed for every offer.');
          if(offers.some(o=>o.min_order!==null&&o.min_order>1))clauses.push(hindi?'Bulk quote ko single-piece sourcing ka offer na maanein.':'A bulk quote is not a single-piece sourcing offer.');
          if(groups.some(s=>[...s.company_workflows,...s.product_offers].some(r=>!r.current)))clauses.push(hindi?'Supplier ki kuch jankari purani hai aur dobara check karni hai.':'Some supplier information is older and needs checking.');}
        const dispatch=new Set(legacy.map(s=>s.single_piece_dispatch));
        if(dispatch.has('not_stated'))clauses.push(hindi?'Kuch supplier leads ne single-piece dispatch ki jankari nahi di hai.':'Single-piece dispatch terms are not stated for some supplier leads.');
        else if(dispatch.size===1&&dispatch.has('not_supported'))clauses.push(hindi?'Di gayi supplier shartein single-piece dispatch support nahi karti hain.':'Listed supplier terms do not support single-piece dispatch.');
        else if(dispatch.size>1)clauses.push(hindi?'Supplier leads ki dispatch shartein alag hain.':'Dispatch terms differ between supplier leads.');
        const subscriptions=new Set(legacy.map(s=>s.subscription));
        if(subscriptions.has('subscription_stated'))clauses.push(hindi?'Kuch supplier leads ne subscription ki zaroorat batayi hai.':'Some supplier leads state a subscription requirement.');
        else if(subscriptions.has('not_stated'))clauses.push(hindi?'Kuch supplier leads ne subscription ki shartein nahi batayi hain.':'Subscription terms are not stated for some supplier leads.');}
      if(p.prices.some(r=>r.kind==='supplier'&&(r.unit==='bulk_quote'||r.moq>1))||p.suppliers.some(s=>s.min_order>1))clauses.push(hindi?'Bulk quote ko single-piece sourcing ka offer na maanein.':'A bulk quote is not a single-piece sourcing offer.');
      items.push({id:p.concept_id,kind:'product',issue_id:facts.issue_id,language,facts:f,required_limitation:clauses.join(' '),update_fact:update,source_english:english?.products.find(r=>r.concept_id===p.concept_id)??null,criteria:p.criteria,meaning_hash:p.functional_definition.meaning_hash});
    }
    const projected=[];
    check(same(Object.keys(proof.items).sort(),items.map(i=>i.id).sort()),'ADMIN_FUNCTIONAL_WRITING_PROOF_INVALID');
    const projection=(id,a)=>({concept_id:id,...pick(a,['reason','points','limitation','update_sentence','checklist_phrases'])});
    const plain=(s,max)=>typeof s==='string'&&Array.from(s).length<=max&&!/<[^>]*>|https?:\/\/|^\s*[=+@]|```/.test(s)&&!/[\p{Nd}{}]/u.test(s.replace(/\{\{(?:title|rank|kpi_matches|date|retail_price|supplier_price)\}\}/g,''))&&!/\b(profit\w*|margin\w*|rto|guarantee\w*\s+(?:winner|sales|returns)|will sell|zero returns|low returns)\b/i.test(s)&&(!hindi||!/[\u0900-\u097f]/.test(s));
    for(const item of items) {
      const title=item.kind==='report_title';item.allowed_reference_ids=item.facts.map(f=>f.id);
      item.output_template={id:item.id,issue_id:item.issue_id,language,title:title?(hindi?'Product sujhaav':'Product picks'):'',reason:title?'':hindi?'Di gayi jankari ke hisaab se {{title}} ko dekhna useful ho sakta hai.':'The supplied facts make {{title}} worth investigating.',points:[],limitation:item.required_limitation,update_sentence:item.update_fact?(hindi?'Saved research mein nayi useful jankari aayi hai.':'Saved research now includes useful new information.'):'',references:title?[]:['market-label',...(item.update_fact?['update']:[])],checklist_phrases:title?[]:item.criteria.map(r=>({id:r.id,text:''}))};
      if(hindi){const sourceId=title?await key('report-title',[facts.issue_id,'en','writing-input-v1']):item.id, source=english.writing_proof.items[sourceId];check(source&&keys(source.reference,['job_id','item_id','accepted_row_hash','writing_input_hash','binding'])&&source.reference.accepted_row_hash===await digest(source.accepted));const sourceProjection=title?{title:source.accepted.title}:projection(sourceId,source.accepted);check(same(sourceProjection,item.source_english));item.source_english_ref={...source.reference,source_english_hash:await digest(sourceProjection)};}
      const row=proof.items[item.id],a=row?.accepted,r=row?.reference;
      check(keys(row,['reference','accepted'])&&keys(r,['job_id','item_id','accepted_row_hash','writing_input_hash','binding'])&&r.item_id===item.id&&Object.values(r).every(v=>typeof v==='string'&&v.length>0)&&r.accepted_row_hash===await digest(a)&&r.writing_input_hash===await digest([context,item]),'ADMIN_FUNCTIONAL_WRITING_INPUT_CHANGED');
      check(keys(a,['id','issue_id','language','title','reason','points','limitation','update_sentence','references','checklist_phrases'])&&a.id===item.id&&a.issue_id===facts.issue_id&&a.language===language&&plain(a.title,100)&&plain(a.reason,450)&&plain(a.limitation,1000)&&plain(a.update_sentence,250)&&Array.isArray(a.points)&&a.points.length<=3&&a.points.every(p=>plain(p,220)&&text(p))&&Array.isArray(a.references)&&new Set(a.references).size===a.references.length&&a.references.every(r=>item.allowed_reference_ids.includes(r)),'ADMIN_FUNCTIONAL_WRITING_OUTPUT_CHANGED');
      if(title)check(text(a.title)&&!a.title.includes('{{')&&!a.reason&&!a.limitation&&!a.update_sentence&&!a.points.length&&!a.references.length&&same(a.checklist_phrases,[])&&a.title===narrative.title);
      else {
        const commercial=/\b(suppliers?|dispatch|subscription|moq|cod)\b[^.!?]{0,80}\b(confirm\w*|support\w*|available|included|free|required|offers?|provides?|allows?|unlimited)\b|\b(confirmed|verified|free)\b[^.!?]{0,30}\b(dispatch|subscription|cod)\b/i;
        check(!a.title&&a.reason.includes('{{title}}')&&a.references.length&&a.limitation===item.required_limitation&&Boolean(a.update_sentence)===Boolean(item.update_fact)&&(!a.update_sentence||a.references.includes('update'))&&!commercial.test([a.reason,...a.points].join(' '))&&Array.isArray(a.checklist_phrases)&&same(a.checklist_phrases.map(r=>r.id).sort((a,b)=>a-b),FIT_IDS)&&a.checklist_phrases.every(r=>keys(r,['id','text'])&&text(r.text)&&plain(r.text,220)),'ADMIN_FUNCTIONAL_WRITING_OUTPUT_CHANGED');
        projected.push(projection(item.id,a));
      }
    }
    check(same(projected,narrative.products),'ADMIN_FUNCTIONAL_WRITING_DISPLAY_CHANGED');
  }
}
