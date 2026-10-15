// Mirrors product_radar/opportunity_score.py: the opportunity score out of 100
// (owner's weights, 15 October 2026). Saved-only: the stored record is re-run
// from its own sub-point facts with the same settings and must match exactly.
// SETTINGS mirrors config/research/opportunity_score.json (a Python test keeps
// them equal); a new settings version needs this mirror and a portal deploy.
export const POLICY_ID = 'opportunity-score-v3';
export const SETTINGS = Object.freeze({
  version: 'opportunity-score-settings-v1',
  parts: { want: 20, early: 30, room: 25, product: 25 },
  want: {
    facebook_ads: { positive_with_advertisers: { min_advertisers: 5, points: 8 }, positive: 6, relevant_ads: 2 },
    google_searches: { nonzero_points: [[4, 5], [1, 2]] },
    platform_lists: { on_list_platforms: [[2, 5], [1, 4]], listed_platforms: [[2, 2], [1, 1]] },
    quick_commerce: { stock_cities: [[2, 2], [1, 1]] },
  },
  early: {
    rising_searches: { rising: 8, strong: 12, strong_rise_percent: 50 },
    new_advertisers: { recent_start_advertisers: [[3, 9], [2, 6], [1, 3]] },
    new_arrivals: { new_arrival_platforms: [[2, 9], [1, 6]] },
  },
  room: { low: 25, medium: 12, high: 0 },
  product: { base_meets: 5, base_points: 10, per_extra_meet: 3 },
  eligibility: { min_meets: 5, min_want: 5 },
  labels: { early_bet_min_early: 15, early_bet_want_below: 10 },
  selection: { high_crowding_only_below: 10 },
});
export const PARTS = ['want', 'early', 'room', 'product'];
export const ITEMS = { want: ['facebook_ads', 'google_searches', 'platform_lists', 'quick_commerce'],
  early: ['rising_searches', 'new_advertisers', 'new_arrivals'], room: ['crowding'], product: ['checklist'] };
export const QUICK_COMMERCE = ['blinkit', 'zepto', 'instamart'];
const MARKET_ITEMS = { facebook_ads: 'meta', new_advertisers: 'meta', google_searches: 'google-trends', rising_searches: 'google-trends', quick_commerce: null };
const LEVELS = ['low', 'medium', 'high', 'not_checked'];
const FACT_FIELDS = { facebook_ads: ['relevant_ads', 'positive', 'advertisers'], google_searches: ['usable', 'nonzero_points'],
  platform_lists: ['presence_at', 'on_list', 'listed'], quick_commerce: ['stock_cities'],
  rising_searches: ['rising', 'previous_mean_upper', 'recent_mean_lower', 'breakout_queries'],
  new_advertisers: ['recent_start_advertisers'], new_arrivals: ['presence_at', 'new_arrival'],
  crowding: ['level', 'rule', 'thresholds_version', 'counts'], checklist: ['meets'] };
export const LABEL_TEXT = { en: { too_quiet: 'Too quiet', proven_but_crowded: 'Proven but crowded', early_bet: 'Early bet', sweet_spot: 'Sweet spot' },
  'hi-Latn': { too_quiet: 'Abhi bahut shaant', proven_but_crowded: 'Bikta hai, par bheed zyada', early_bet: 'Jaldi ka daav', sweet_spot: 'Sahi mauka' } };
const PART_TEXT = { en: { want: 'Want', early: 'Early', room: 'Room', product: 'Product' }, 'hi-Latn': { want: 'Maang', early: 'Jaldi', room: 'Jagah', product: 'Product' } };
const LEVEL_TEXT = { en: { low: 'Low', medium: 'Medium', high: 'High', not_checked: 'Not checked' },
  'hi-Latn': { low: 'Kam', medium: 'Madhyam', high: 'Zyada', not_checked: 'Abhi check nahi hui' } };
const ID = /^[A-Za-z0-9_-]{1,80}$/;
const DATE = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/;
// Python opportunity_score._utc_at: a real calendar instant written in UTC (Z or +00:00).
export const utcAt = value => {
  if (typeof value !== 'string' || !DATE.test(value)) return false;
  const at = Date.parse(value);
  return Number.isFinite(at) && new Date(at).toISOString().slice(0, 19) === value.slice(0, 19);
};
// Saved facts the score reads (Python platform_roles / crowding; a Python test keeps them equal).
export const PRESENCE = Object.freeze({ kind: 'dropship-platform-presence', version: 'dropship-presence-v2', days: 7,
  routes: ['deodap', 'baapstore', 'dropship-india', 'dropdash'],
  states: ['relevant', 'relevant_delisted', 'no_relevant_items', 'no_relevant_items_in_sample', 'review_pending', 'not_checked'] });
// thresholds mirrors config/research/crowding.json (its version is required of every frozen fact).
export const CROWDING = Object.freeze({ kind: 'crowding-fact-v1', version: 'crowding-v2', parser_version: 3,
  thresholds: { version: 'crowding-thresholds-v1',
    high: { bestseller_hits: 1, rated_listings: 3, rated_min_ratings: 1000, listings: 8, brands: 5, bought_listings: 2, bought_min_per_month: 1000 },
    low: { max_listings: 2, ratings_below: 200 } } });
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const count = value => Number.isSafeInteger(value) && value >= 0;
const points = (ladder, n) => (ladder.find(([minimum]) => n >= minimum) ?? [0, 0])[1];
const union = value => [...new Set(Object.values(value).flat())].sort();
const ids = value => Array.isArray(value) && value.every(v => typeof v === 'string' && ID.test(v)) && JSON.stringify(value) === JSON.stringify([...new Set(value)].sort());
const routes = value => object(value) && Object.entries(value).every(([name, refs]) => name && refs.length && ids(refs));
// A chart mean bound as exact text: Python's normalised Decimal of a quarter point.
export const meanText = value => value === null || value === undefined ? null : String(value);

function itemPoints(name, f) {
  const want = SETTINGS.want, early = SETTINGS.early;
  if (name === 'facebook_ads') { const r = want.facebook_ads; return f.positive && (f.advertisers ?? 0) >= r.positive_with_advertisers.min_advertisers ? r.positive_with_advertisers.points : f.positive ? r.positive : f.relevant_ads ? r.relevant_ads : 0; }
  if (name === 'google_searches') return f.usable && f.nonzero_points !== null ? points(want.google_searches.nonzero_points, f.nonzero_points) : 0;
  if (name === 'platform_lists') return points(want.platform_lists.on_list_platforms, Object.keys(f.on_list).length) || points(want.platform_lists.listed_platforms, Object.keys(f.listed).length);
  if (name === 'quick_commerce') return points(want.quick_commerce.stock_cities, Object.keys(f.stock_cities).length);
  if (name === 'rising_searches') {
    const r = early.rising_searches; if (!f.rising) return 0;
    const before = f.previous_mean_upper === null ? null : Number(f.previous_mean_upper), now = f.recent_mean_lower === null ? null : Number(f.recent_mean_lower);
    // Quarter points times whole percentages are exact binary numbers.
    return f.breakout_queries > 0 || before !== null && now !== null && before > 0 && now * 100 >= before * (100 + r.strong_rise_percent) ? r.strong : r.rising;
  }
  if (name === 'new_advertisers') return points(early.new_advertisers.recent_start_advertisers, f.recent_start_advertisers ?? 0);
  if (name === 'new_arrivals') return points(early.new_arrivals.new_arrival_platforms, Object.keys(f.new_arrival).length);
  if (name === 'crowding') return f.level === 'not_checked' ? 0 : SETTINGS.room[f.level];
  const p = SETTINGS.product; return f.meets >= p.base_meets ? p.base_points + p.per_extra_meet * (f.meets - p.base_meets) : 0;
}

function itemEvidence(name, f, earned, evidence) {
  if (name === 'crowding') return evidence;
  if (!earned) return [];
  if (name === 'platform_lists') return Object.keys(f.on_list).length ? union(f.on_list) : union(f.listed);
  if (name === 'new_arrivals') return union(f.new_arrival);
  if (name === 'quick_commerce') return union(f.stock_cities);
  return evidence;
}

// inputs: {item: [facts, evidence ids]} -> the record Python's _assemble builds.
export function assembleScore(inputs) {
  const parts = {};
  for (const part of PARTS) {
    const items = {};
    for (const name of ITEMS[part]) { const [facts, evidence] = inputs[name], earned = itemPoints(name, facts); items[name] = { points: earned, facts, evidence_ids: [...itemEvidence(name, facts, earned, evidence)] }; }
    parts[part] = { points: Object.values(items).reduce((a, row) => a + row.points, 0), max: SETTINGS.parts[part], items };
  }
  const total = PARTS.reduce((a, part) => a + parts[part].points, 0), want = parts.want.points, early = parts.early.points;
  const level = inputs.crowding[0].level, meets = inputs.checklist[0].meets, rules = SETTINGS.eligibility, labels = SETTINGS.labels;
  const label = want < rules.min_want ? 'too_quiet' : level === 'not_checked' ? null : level === 'high' ? 'proven_but_crowded'
    : early >= labels.early_bet_min_early && want < labels.early_bet_want_below ? 'early_bet' : 'sweet_spot';
  const reason = meets < rules.min_meets ? 'fit_information_pending' : level === 'not_checked' ? 'crowding_pending' : want < rules.min_want ? 'demand_too_quiet' : 'opportunity_scored';
  return { opportunity_policy_id: POLICY_ID, opportunity_score: total, opportunity_label: label,
    opportunity_basis: { settings_version: SETTINGS.version, parts, eligible: reason === 'opportunity_scored', reason } };
}

function checkedFacts(name, f, invalid) {
  if (!object(f) || JSON.stringify(Object.keys(f).sort()) !== JSON.stringify([...FACT_FIELDS[name]].sort())) invalid();
  const flags = { facebook_ads: ['positive'], google_searches: ['usable'], rising_searches: ['rising'] }[name] ?? [];
  const counts = { facebook_ads: ['relevant_ads', 'advertisers'], google_searches: ['nonzero_points'], rising_searches: ['breakout_queries'], new_advertisers: ['recent_start_advertisers'], checklist: ['meets'] }[name] ?? [];
  if (flags.some(k => typeof f[k] !== 'boolean')) invalid();
  for (const k of counts) { const required = ['relevant_ads', 'breakout_queries', 'meets'].includes(k); if (f[k] === null ? required : !count(f[k]) || k === 'meets' && f[k] > 10) invalid(); }
  if (name === 'rising_searches') for (const k of ['previous_mean_upper', 'recent_mean_lower']) if (f[k] !== null && (typeof f[k] !== 'string' || !/^(?:0|[1-9]\d*)(?:\.(?:25|5|75))?$/.test(f[k]))) invalid();
  if (['platform_lists', 'new_arrivals'].includes(name) && f.presence_at !== null && !utcAt(f.presence_at)) invalid();
  if (name === 'quick_commerce' && !routes(f.stock_cities)) invalid();
  if (name === 'platform_lists' && (!routes(f.listed) || !routes(f.on_list))) invalid();
  if (name === 'new_arrivals' && !routes(f.new_arrival)) invalid();
  if (name === 'crowding') {
    const known = f.level !== 'not_checked';
    if (!LEVELS.includes(f.level) || known !== (typeof f.rule === 'string') || typeof f.thresholds_version !== 'string' || f.counts !== null && !object(f.counts)) invalid();
  }
  return f;
}

// Python validate_record: shape, the arithmetic and the label from the stored facts.
export function validateScoreRecord(record, matches, fail) {
  const invalid = (code = 'ADMIN_OPPORTUNITY_BASIS_INVALID') => fail(code, 409);
  if (!object(record) || record.opportunity_policy_id !== POLICY_ID) invalid('ADMIN_OPPORTUNITY_POLICY_MISMATCH');
  // Stored points are whole numbers: a boolean, string or fraction is refused outright.
  if (!Number.isInteger(record.opportunity_score)) invalid('ADMIN_OPPORTUNITY_SCORE_INVALID');
  const basis = record.opportunity_basis;
  if (!object(basis) || basis.settings_version !== SETTINGS.version) invalid('ADMIN_OPPORTUNITY_SETTINGS_VERSION_MISMATCH');
  if (JSON.stringify(Object.keys(basis).sort()) !== JSON.stringify(['eligible', 'parts', 'reason', 'settings_version']) || !object(basis.parts) ||
      JSON.stringify(Object.keys(basis.parts).sort()) !== JSON.stringify([...PARTS].sort())) invalid();
  const inputs = {};
  for (const part of PARTS) {
    const row = basis.parts[part];
    if (!object(row) || JSON.stringify(Object.keys(row).sort()) !== JSON.stringify(['items', 'max', 'points']) || !object(row.items) ||
        !Number.isInteger(row.points) || !Number.isInteger(row.max) ||
        JSON.stringify(Object.keys(row.items).sort()) !== JSON.stringify([...ITEMS[part]].sort())) invalid();
    for (const name of ITEMS[part]) {
      const item = row.items[name];
      if (!object(item) || JSON.stringify(Object.keys(item).sort()) !== JSON.stringify(['evidence_ids', 'facts', 'points']) || !ids(item.evidence_ids) ||
          !Number.isInteger(item.points)) invalid();
      inputs[name] = [checkedFacts(name, item.facts, invalid), item.evidence_ids];
    }
  }
  const lists = inputs.platform_lists[0], arrivals = inputs.new_arrivals[0];
  if (lists.presence_at !== arrivals.presence_at || lists.presence_at === null && (Object.keys(lists.listed).length || Object.keys(lists.on_list).length || Object.keys(arrivals.new_arrival).length) ||
      [lists.on_list, arrivals.new_arrival].some(value => Object.entries(value).some(([route, refs]) => !lists.listed[route] || refs.some(ref => !lists.listed[route].includes(ref))))) invalid();
  const meta = inputs.facebook_ads[1];
  if (meta.length && meta.length !== inputs.facebook_ads[0].relevant_ads || inputs.new_advertisers[1].length && JSON.stringify(inputs.new_advertisers[1]) !== JSON.stringify(meta) ||
      inputs.rising_searches[1].length && inputs.google_searches[1].length && JSON.stringify(inputs.rising_searches[1]) !== JSON.stringify(inputs.google_searches[1])) invalid('ADMIN_OPPORTUNITY_SOURCE_REFERENCE_INVALID');
  if (matches !== undefined && inputs.checklist[0].meets !== matches) invalid('ADMIN_OPPORTUNITY_FIT_INVALID');
  const expected = assembleScore(inputs);
  if (JSON.stringify(canonicalOrder(basis)) !== JSON.stringify(canonicalOrder(expected.opportunity_basis)) || record.opportunity_score !== expected.opportunity_score) invalid('ADMIN_OPPORTUNITY_SCORE_INVALID');
  if (record.opportunity_label !== expected.opportunity_label) invalid('ADMIN_OPPORTUNITY_LABEL_INVALID');
  return expected;
}

function canonicalOrder(value) {
  if (Array.isArray(value)) return value.map(canonicalOrder);
  if (!object(value)) return value;
  return Object.fromEntries(Object.keys(value).sort().map(k => [k, canonicalOrder(value[k])]));
}

export const crowdingLevel = record => record.opportunity_basis.parts.room.items.crowding.facts.level;

// Python platform_roles: the "moving" dates a presence fact's change hash leaves out.
export const presenceChangeBasis = value => Array.isArray(value) ? value.map(presenceChangeBasis) : object(value)
  ? Object.fromEntries(Object.entries(value).filter(([k]) => !['last_seen_at', 'as_of', 'at'].includes(k)).map(([k, v]) => [k, presenceChangeBasis(v)])) : value;

// Python platform_roles._valid_presence (without the change hash, which needs a digest).
export const presenceShapeValid = (record, conceptId, meaningHash) => object(record) && record.kind === PRESENCE.kind &&
  record.version === PRESENCE.version && record.concept_id === conceptId && record.meaning_hash === meaningHash && utcAt(record.at) &&
  typeof record.change_hash === 'string' && object(record.platforms) &&
  JSON.stringify(Object.keys(record.platforms).sort()) === JSON.stringify([...PRESENCE.routes].sort()) &&
  Object.values(record.platforms).every(row => object(row) && PRESENCE.states.includes(row.state));

// Python opportunity_score.presence_input: listed items per platform; older than a week (or later than the score) is not current.
export function presenceInput(record, asOf) {
  if (record === null) return null;
  const at = Date.parse(record.at), now = Date.parse(asOf);
  if (at > now || now - at > PRESENCE.days * 86400000) return null;
  const result = { at: record.at, listed: {}, on_list: {}, new_arrival: {} };
  for (const route of record.platforms_carrying) {
    const items = record.platforms[route].items.filter(row => row.listed);
    for (const [field, wanted] of [['listed', items], ['on_list', items.filter(row => row.on_list)], ['new_arrival', items.filter(row => row.new_arrival)]])
      if (wanted.length) result[field][route] = [...new Set(wanted.map(row => row.observation_id))].sort();
  }
  return result;
}

// Python opportunity_score._presence_facts: the two sub-points' facts (no evidence of their own).
export const presenceFacts = presence => presence === null
  ? [[{ presence_at: null, on_list: {}, listed: {} }, []], [{ presence_at: null, new_arrival: {} }, []]]
  : [[{ presence_at: presence.at, on_list: presence.on_list, listed: presence.listed }, []], [{ presence_at: presence.at, new_arrival: presence.new_arrival }, []]];

const LADDER_COUNTS = ['bestseller_hits', 'listings_rated_high', 'listings_with_brand', 'brands_or_sellers', 'bought_past_month_hits',
  'card_facts_missing', 'listings_rated_200_plus', 'ratings_not_shown'];

// Python crowding.threshold_ladder: [level or null, rule] for a fact's own counts, or null when the counts are malformed.
export function thresholdLadder(c) {
  const high = CROWDING.thresholds.high, low = CROWDING.thresholds.low, listings = object(c) ? c.relevant_listings : null;
  if (!object(c) || !LADDER_COUNTS.every(name => count(c[name])) || !object(listings) ||
      JSON.stringify(Object.keys(listings).sort()) !== '["amazon","flipkart"]' || !Object.values(listings).every(count)) return null;
  if (c.bestseller_hits >= high.bestseller_hits) return ['high', 'bestseller_list'];
  if (c.listings_rated_high >= high.rated_listings) return ['high', 'well_rated_listings'];
  if (c.listings_with_brand >= high.listings && c.brands_or_sellers >= high.brands) return ['high', 'many_brands_shown'];
  if (c.bought_past_month_hits >= high.bought_listings) return ['high', 'bought_past_month'];
  if (c.card_facts_missing) return [null, 'saved_page_unreadable'];
  if (listings.amazon + listings.flipkart <= low.max_listings && c.listings_rated_200_plus === 0 && c.ratings_not_shown === 0) return ['low', 'few_listings'];
  return ['medium', 'default'];
}

// Python crowding.validate_crowding_fact: the level and rule are re-derived from the fact's own counts.
export function crowdingFactValid(fact, conceptId, meaningHash) {
  if (!object(fact) || fact.kind !== CROWDING.kind || fact.version !== CROWDING.version || fact.concept_id !== conceptId ||
      typeof meaningHash !== 'string' || fact.meaning_hash !== meaningHash || fact.parser_version !== CROWDING.parser_version ||
      fact.thresholds_version !== CROWDING.thresholds.version || !LEVELS.includes(fact.level) || !ids(fact.evidence_ids)) return false;
  if (fact.level === 'not_checked') return fact.rule === null && fact.reason !== 'CROWDING_CHECKED' &&
    (fact.counts === null || (thresholdLadder(fact.counts) ?? ['high'])[0] !== 'high');
  const ladder = thresholdLadder(fact.counts);
  return fact.reason === 'CROWDING_CHECKED' && ladder !== null && ladder[0] === fact.level && ladder[1] === fact.rule &&
    fact.counts.relevant_listings.amazon + fact.counts.relevant_listings.flipkart <= fact.evidence_ids.length;
}

// Python opportunity_score.crowding_input as [facts, evidence ids].
export const crowdingFacts = fact => [{ level: fact.level, rule: fact.rule ?? null, thresholds_version: fact.thresholds_version ?? null,
  counts: fact.counts ?? null }, [...(fact.evidence_ids ?? [])].sort()];

// Evidence ids of the market sub-points that earn points, by site ("quick-commerce" for the three apps).
export function marketEvidence(record) {
  const sites = {};
  for (const part of ['want', 'early']) for (const [name, row] of Object.entries(record.opportunity_basis.parts[part].items)) {
    if (!Object.hasOwn(MARKET_ITEMS, name) || !row.points) continue;
    const site = MARKET_ITEMS[name] ?? 'quick-commerce', refs = site === 'quick-commerce' ? Object.values(row.facts.stock_cities).flat() : row.evidence_ids;
    sites[site] = [...new Set([...(sites[site] ?? []), ...refs])].sort();
  }
  return Object.fromEntries(Object.keys(sites).sort().map(site => [site, sites[site]]));
}

// Python validate_issue_crowding: in the issue's order, no High type follows ten Low/Medium ones.
export function issueCrowdingValid(products) {
  let seen = 0;
  for (const p of products) {
    if (p?.opportunity_policy_id !== POLICY_ID) continue;
    if (crowdingLevel(p) === 'high') { if (seen >= SETTINGS.selection.high_crowding_only_below) return false; }
    else seen++;
  }
  return true;
}

const SCORE_PREFIX = { en: 'Opportunity score', 'hi-Latn': 'Mauke ka score' };

export function scoreText(product, language = 'en') {
  const parts = product.opportunity_basis.parts, label = product.opportunity_label;
  return SCORE_PREFIX[language] + ' ' + product.opportunity_score + '/100' + PARTS.map(p => ' · ' + PART_TEXT[language][p] + ' ' + parts[p].points + '/' + parts[p].max).join('') + (label ? ' · ' + LABEL_TEXT[language][label] : '');
}

export function crowdingText(product, language = 'en') {
  const f = product.opportunity_basis.parts.room.items.crowding.facts, c = f.counts;
  const text = (language === 'en' ? 'Amazon/Flipkart crowding: ' : 'Amazon/Flipkart par bheed: ') + LEVEL_TEXT[language][f.level];
  if (f.level === 'not_checked' || !object(c)) return text;
  const listings = c.relevant_listings ?? {}, amazon = listings.amazon ?? 0, flipkart = listings.flipkart ?? 0, ratings = c.max_ratings ?? null;
  const branded = c.listings_with_brand || 0, hidden = c.brand_not_shown || 0;  // never "0 brands" when brands are not shown
  const brands = language === 'en'
    ? (!branded ? 'brands not shown' : `${c.brands_or_sellers ?? 0} brands or sellers` + (hidden ? ` (${hidden} show no brand)` : ''))
    : (!branded ? 'brand nahi dikhe' : `${c.brands_or_sellers ?? 0} brand ya seller` + (hidden ? ` (${hidden} listing par brand nahi dikha)` : ''));
  const details = language === 'en'
    ? [`${amazon + flipkart} relevant listings: Amazon ${amazon}, Flipkart ${flipkart}`, brands, 'most ratings ' + (ratings !== null ? String(ratings) : 'not shown'), `${c.bestseller_hits ?? 0} on a bestseller list`]
    : [`${amazon + flipkart} milti-julti listing: Amazon ${amazon}, Flipkart ${flipkart}`, brands, 'sabse zyada ratings ' + (ratings !== null ? String(ratings) : 'nahi dikhi'), `${c.bestseller_hits ?? 0} bestseller list par`];
  return text + ' (' + details.join('; ') + ')';
}

// Python meta_facts.depth_facts recent_start_advertisers over the portal's Meta rows.
export function metaRecentStarts(rows, utc) {
  const parent = new Map(), find = name => { if (!parent.has(name)) parent.set(name, name); while (parent.get(name) !== name) { parent.set(name, parent.get(parent.get(name))); name = parent.get(name); } return name; };
  for (const r of rows) { const names = [r.page_id, r.page_address].filter(v => v !== null); for (const n of names) find(n); if (names.length === 2) parent.set(find(names[0]), find(names[1])); }
  const grouped = new Map();
  for (const n of [...parent.keys()].sort()) { const root = find(n); if (!grouped.has(root)) grouped.set(root, new Set()); grouped.get(root).add(n); }
  const address = n => n.startsWith('page-url:'), groups = [...grouped.values()];
  const ambiguous = groups.some(g => [...g].every(address)) && groups.some(g => ![...g].some(address));
  let recent = null;
  for (const group of groups) {
    if (ambiguous && [...group].every(address)) continue;
    const own = rows.filter(r => group.has(r.page_id) || group.has(r.page_address));
    if (own.some(r => r.started === null)) continue;
    // Python: the earliest start, then the earliest observation, then the id.
    const first = own.reduce((a, b) => (utc(b.started) - utc(a.started) || utc(b.observed) - utc(a.observed) || (b.id < a.id ? -1 : b.id > a.id ? 1 : 0)) < 0 ? b : a);
    const age = utc(first.observed) - utc(first.started);
    recent = (recent ?? 0) + (age >= 0 && age <= 30 * 86400000 ? 1 : 0);
  }
  return recent;
}
