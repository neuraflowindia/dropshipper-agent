// Pure/bounded projections over the current trusted release root. No provider,
// account, model or public artifact fallback exists in this module.
import { ReleaseTree, ReleasePageError, RELEASE_KINDS, emptyReleaseRoot, releaseDigest, releaseBytes,
  releaseSequenceKey, releaseQueueKey, releaseHoldKey, releaseTime, releaseCanonical } from './release-pages.mjs';
const ID = /^[A-Za-z0-9_-]{1,80}$/, HASH = /^[a-f0-9]{64}$/;
const fail = (code, status = 409) => { throw new ReleasePageError(code, status); };
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
export const timelineKey = entry => releaseTime(entry.scheduled_at).toString(16).padStart(16, '0') + releaseSequenceKey(entry.sequence);
export const revisionKey = async (issue, revision) => await releaseDigest(issue) + releaseSequenceKey(revision);
export function firstAvailability(entry, at) {
  if (!entry.approved || !entry.complete) return null;
  const start = Math.max(releaseTime(entry.scheduled_at), releaseTime(entry.server_activated_at));
  const end = entry.server_deactivated_at == null ? null : releaseTime(entry.server_deactivated_at);
  return start <= releaseTime(at) && (end === null || end > start) ? new Date(start).toISOString() : null;
}
export const visibleRelease = (entry, at) => entry.remote_state === 'active' && firstAvailability(entry, at) !== null;
export function releaseConcepts(entry) {
  if (!Array.isArray(entry.concept_ids) || entry.concept_ids.length !== entry.product_count || new Set(entry.concept_ids).size !== entry.concept_ids.length || entry.concept_ids.some(id => !ID.test(id))) fail('RELEASE_HISTORY_CONCEPTS_REQUIRED');
  return entry.concept_ids;
}
export function publicationRef(entry) {
  return { issue_id: entry.issue_id, sequence: entry.sequence, revision: entry.revision, package_hash: entry.package_hash,
    selection_hash: entry.selection_hash, facts_hash: entry.facts_hash ?? null,
    available_at: new Date(Math.max(releaseTime(entry.scheduled_at), releaseTime(entry.server_activated_at))).toISOString() };
}
function emptyIssue(id) { return { issue_id: id, latest_sequence: null, active_sequence: null, publication_ordinal: null, first_exposed_at: null }; }
function emptyType(id) { return { concept_id: id, first_exposed_at: null, first_publication: null, latest_publication: null }; }
function updateExposure(type, entry, at) {
  const ref = publicationRef(entry), begin = firstAvailability(entry, at);
  if (begin === null) return;
  const order = value => [releaseTime(value.available_at), value.sequence];
  const before = (a, b) => { const x = order(a), y = order(b); return x[0] < y[0] || x[0] === y[0] && x[1] < y[1]; };
  if (!type.first_publication || before(ref, type.first_publication)) { type.first_publication = ref; type.first_exposed_at = begin; }
  if (!type.latest_publication || before(type.latest_publication, ref)) type.latest_publication = ref;
}
export function recommendationsFromFacts(entry, facts) {
  if (!facts || !Array.isArray(facts.products) || facts.issue_id !== entry.issue_id || facts.revision !== entry.revision ||
      facts.selection_hash !== entry.selection_hash || !same(facts.products.map(row => row?.concept_id), releaseConcepts(entry))) fail('RELEASE_HISTORY_FACTS_REQUIRED');
  return facts.products.map(row => {
    if (!['New', 'Updated'].includes(row.status)) fail('RELEASE_HISTORY_FACTS_REQUIRED');
    return { concept_id: row.concept_id, status: row.status, meaning_hash: row.functional_definition?.meaning_hash ?? null };
  });
}

// Also used by the isolated 2,000-issue fixture. It is not a migration authority:
// the actual v1 switch separately validates its source ETag and original root.
export async function buildReleaseHistory(entries, { client, asOf, migration = null, factsByRevision = {}, writeLimit = 12000 } = {}) {
  const root = emptyReleaseRoot(client, asOf, migration), tree = new ReleaseTree(null, root, { writeLimit });
  const tables = Object.fromEntries(RELEASE_KINDS.map(kind => [kind, new Map()])), issues = new Map(), types = new Map(), publishedOrdinals = new Map();
  let sequence = 0;
  for (const original of entries) {
    const entry = { ...structuredClone(original), sequence: ++sequence }, ids = releaseConcepts(entry), begin = firstAvailability(entry, asOf);
    const issue = issues.get(entry.issue_id) ?? emptyIssue(entry.issue_id); issues.set(entry.issue_id, issue);
    issue.latest_sequence = sequence;
    if (entry.remote_state === 'active') { if (issue.active_sequence !== null) fail('RELEASE_ACTIVE_REVISION_CONFLICT'); issue.active_sequence = sequence; }
    if (begin !== null) {
      if (issue.publication_ordinal !== null && issue.publication_ordinal !== entry.publication_ordinal ||
          publishedOrdinals.has(entry.publication_ordinal) && publishedOrdinals.get(entry.publication_ordinal) !== entry.issue_id) fail('RELEASE_PUBLISHED_ORDINAL_CONFLICT');
      issue.publication_ordinal = entry.publication_ordinal; publishedOrdinals.set(entry.publication_ordinal, entry.issue_id);
      if (issue.first_exposed_at === null || releaseTime(begin) < releaseTime(issue.first_exposed_at)) issue.first_exposed_at = begin;
    }
    for (const id of ids) { const type = types.get(id) ?? emptyType(id); updateExposure(type, entry, asOf); types.set(id, type); }
    if (entry.remote_state === 'active' && entry.approved && entry.complete) {
      tables.timeline.set(timelineKey(entry), { issue_id: entry.issue_id, sequence });
      if (begin === null) {
        const facts = factsByRevision[entry.issue_id + ':' + entry.revision];
        entry.recommendations = recommendationsFromFacts(entry, facts);
        tables.queue.set(await releaseQueueKey(entry), { issue_id: entry.issue_id, sequence, available_at: publicationRef(entry).available_at,
          scheduled_at: entry.scheduled_at, publication_ordinal: entry.publication_ordinal, is_new_issue: issue.first_exposed_at === null });
        for (const row of entry.recommendations) tables.holds.set(await releaseHoldKey(row.concept_id, entry.issue_id), { concept_id: row.concept_id, issue_id: entry.issue_id, sequence, status: row.status });
      }
    }
    tables.archive.set(releaseSequenceKey(sequence), entry);
    const revision = await revisionKey(entry.issue_id, entry.revision);
    if (tables.revisions.has(revision)) fail('RELEASE_REVISION_CONFLICT');
    tables.revisions.set(revision, { issue_id: entry.issue_id, revision: entry.revision, sequence });
  }
  for (const [id, value] of issues) tables.issues.set(await releaseDigest(id), value);
  for (const [id, value] of types) tables.types.set(await releaseDigest(id), value);
  // Earlier revisions may establish exposure after their active replacement was
  // visited. Derive new-issue queue flags only after all original intervals.
  for (const [key, value] of tables.queue) value.is_new_issue = issues.get(value.issue_id).first_exposed_at === null;
  for (const kind of Object.keys(root.pages)) root.pages[kind] = await tree.build(kind, [...tables[kind]].map(([key, value]) => ({ key, value })).sort((a, b) => a.key.localeCompare(b.key)));
  root.next_sequence = sequence + 1; root.settled_publication_count = [...issues.values()].filter(row => row.first_exposed_at !== null).length;
  return { root, tree, objects: [...tree.pending].map(([key, value]) => ({ key, ...value.ref, bytes: value.bytes })) };
}

export async function archiveEntry(tree, sequence, validateEntry, at) {
  if (!Number.isSafeInteger(sequence) || sequence < 1 || sequence >= tree.root.next_sequence) fail('RELEASE_ENTRY_POINTER_INVALID');
  const entry = await tree.get('archive', releaseSequenceKey(sequence));
  if (!entry || entry.sequence !== sequence) fail('RELEASE_ENTRY_POINTER_INVALID');
  validateEntry(entry, at); return entry;
}
export async function issueProjection(tree, id, validateEntry, at) {
  const row = await tree.get('issues', await releaseDigest(id));
  if (!row) return null;
  const fields = ['issue_id', 'latest_sequence', 'active_sequence', 'publication_ordinal', 'first_exposed_at'];
  if (!same(Object.keys(row).sort(), fields.sort()) || row.issue_id !== id || !Number.isSafeInteger(row.latest_sequence) || row.latest_sequence < 1 ||
      !(row.active_sequence === null || Number.isSafeInteger(row.active_sequence) && row.active_sequence > 0) ||
      !(row.publication_ordinal === null || Number.isSafeInteger(row.publication_ordinal) && row.publication_ordinal > 0) ||
      (row.first_exposed_at === null) !== (row.publication_ordinal === null)) fail('RELEASE_ISSUE_PROJECTION_INVALID');
  if (row.first_exposed_at !== null && releaseTime(row.first_exposed_at) > releaseTime(at)) fail('RELEASE_ISSUE_PROJECTION_INVALID');
  const latest = await archiveEntry(tree, row.latest_sequence, validateEntry, at);
  const active = row.active_sequence === null ? null : await archiveEntry(tree, row.active_sequence, validateEntry, at);
  if (latest.issue_id !== id || active && (active.issue_id !== id || active.remote_state !== 'active') || latest.remote_state === 'active' && row.active_sequence !== row.latest_sequence) fail('RELEASE_ISSUE_PROJECTION_INVALID');
  return { row, latest, active };
}
export async function revisionProjection(tree, id, revision, validateEntry, at) {
  const ref = await tree.get('revisions', await revisionKey(id, revision));
  if (!ref) return null;
  if (ref.issue_id !== id || ref.revision !== revision || Object.keys(ref).sort().join('|') !== 'issue_id|revision|sequence') fail('RELEASE_REVISION_POINTER_INVALID');
  const entry = await archiveEntry(tree, ref.sequence, validateEntry, at);
  if (entry.issue_id !== id || entry.revision !== revision) fail('RELEASE_REVISION_POINTER_INVALID');
  return entry;
}
export async function visibleReleasePage(tree, { after = null, limit = 20, at, validateEntry } = {}) {
  const before = (releaseTime(at) + 1).toString(16).padStart(16, '0') + '0'.repeat(16);
  const scan = await tree.scan('timeline', { after, before, limit, reverse: true });
  const rows = [];
  for (const item of scan.rows) {
    const entry = await archiveEntry(tree, item.value.sequence, validateEntry, at);
    if (entry.issue_id !== item.value.issue_id || timelineKey(entry) !== item.key || entry.remote_state !== 'active') fail('RELEASE_TIMELINE_BINDING_INVALID');
    if (visibleRelease(entry, at)) rows.push(entry);
  }
  return { ...scan, entries: rows };
}

// A due queue entry is already owner-approved. Settling records real elapsed
// availability; it does not publish, recollect, change dates or delete history.
export async function settleReleaseQueue(tree, { at, validateEntry, limit = 20 } = {}) {
  const before = (releaseTime(at) + 1).toString(16).padStart(16, '0') + '0'.repeat(64);
  const scan = await tree.scan('queue', { before, limit });
  for (const item of scan.rows) {
    const queued = item.value, entry = await archiveEntry(tree, queued.sequence, validateEntry, at);
    const issue = await issueProjection(tree, entry.issue_id, validateEntry, at);
    if (!issue || issue.row.active_sequence !== entry.sequence || entry.remote_state !== 'active' || queued.issue_id !== entry.issue_id || await releaseQueueKey(entry) !== item.key || firstAvailability(entry, at) === null) fail('RELEASE_QUEUE_BINDING_INVALID');
    if (issue.row.first_exposed_at === null) { issue.row.first_exposed_at = firstAvailability(entry, at); issue.row.publication_ordinal = entry.publication_ordinal; tree.root.settled_publication_count++; }
    await tree.set('issues', await releaseDigest(entry.issue_id), issue.row);
    for (const id of releaseConcepts(entry)) {
      const type = await tree.get('types', await releaseDigest(id)) ?? emptyType(id);
      if (type.concept_id !== id) fail('RELEASE_TYPE_PROJECTION_INVALID'); updateExposure(type, entry, at);
      await tree.set('types', await releaseDigest(id), type); await tree.set('holds', await releaseHoldKey(id, entry.issue_id), null);
    }
    await tree.set('queue', item.key, null);
  }
  return { settled: scan.rows.length, remaining: scan.more, next_cursor: scan.more ? scan.last_key : null };
}

function identityCache(tree){
  const key=(tree.root.pages.aliases?.sha256??'-')+':'+(tree.root.pages.lineage?.sha256??'-');
  if(tree.identityCache?.key!==key)tree.identityCache={key,aliases:new Map(),neighbors:new Map(),contexts:new Map()};
  return tree.identityCache;
}
export async function canonicalType(tree, id) {
  if(!ID.test(id))fail('RELEASE_ALIAS_PROJECTION_INVALID');
  const cache=identityCache(tree).aliases;if(cache.has(id))return structuredClone(cache.get(id));
  const alias = await tree.get('aliases', await releaseDigest(id));
  if (!alias) {const plain={ canonical_id: id, member_ids: [id], record_refs: [], ancestor_ids: [] };cache.set(id,plain);return structuredClone(plain);}
  const fields=['member_id','canonical_id','member_ids','record_refs','ancestor_ids'];
  const validate=(row,member)=>{
    if(!row||!same(Object.keys(row).sort(),[...fields].sort())||row.member_id!==member||!ID.test(row.canonical_id??'')||!Array.isArray(row.member_ids)||!row.member_ids.includes(member)||row.canonical_id!==row.member_ids[0]||
       row.member_ids.some(v=>!ID.test(v))||row.member_ids.join('|')!==[...new Set(row.member_ids)].sort().join('|')||!Array.isArray(row.ancestor_ids)||row.ancestor_ids.length||
       !Array.isArray(row.record_refs)||row.record_refs.some(ref=>Object.keys(ref??{}).sort().join('|')!=='hash|id'||!ID.test(ref.id??'')||!HASH.test(ref.hash??''))||
       row.record_refs.map(ref=>ref.id).join('|')!==[...new Set(row.record_refs.map(ref=>ref.id))].sort().join('|'))fail('RELEASE_ALIAS_PROJECTION_INVALID');
  };
  validate(alias,id);
  for(const member of alias.member_ids){const peer=member===id?alias:await tree.get('aliases',await releaseDigest(member));validate(peer,member);if(peer.canonical_id!==alias.canonical_id||!same(peer.member_ids,alias.member_ids)||!same(peer.record_refs,alias.record_refs))fail('RELEASE_ALIAS_PROJECTION_INVALID');}
  cache.set(id,alias);return structuredClone(alias);
}
export const lineageKey = async(parent,child,direction=0) => {
  if(!ID.test(parent)||!ID.test(child)||parent===child||![0,1].includes(direction))fail('RELEASE_LINEAGE_KEY_INVALID');
  return await releaseDigest(direction===0?parent:child)+String(direction)+await releaseDigest(direction===0?child:parent);
};
export async function validateLineageAdjacency(tree,item,id,direction){
  const r=item?.value,fields=['parent_id','child_id','record_refs'];
  if(!r||!same(Object.keys(r).sort(),fields.sort())||!ID.test(r.parent_id??'')||!ID.test(r.child_id??'')||r.parent_id===r.child_id||
     (direction===0?r.parent_id:r.child_id)!==id||item.key!==await lineageKey(r.parent_id,r.child_id,direction)||!Array.isArray(r.record_refs)||!r.record_refs.length||
     r.record_refs.some(ref=>!ref||typeof ref!=='object'||Array.isArray(ref)||!ID.test(ref.id??'')||!HASH.test(ref.hash??'')||Object.keys(ref).sort().join('|')!=='hash|id')||
     r.record_refs.map(ref=>ref.id).join('|')!==[...new Set(r.record_refs.map(ref=>ref.id))].sort().join('|'))fail('RELEASE_LINEAGE_PROJECTION_INVALID');
  const mirrored=await tree.get('lineage',await lineageKey(r.parent_id,r.child_id,1-direction));
  if(!mirrored||releaseCanonical(mirrored)!==releaseCanonical(r))fail('RELEASE_LINEAGE_MIRROR_INVALID');
  return r;
}
export async function lineageNeighbors(tree,id,direction){
  if(!ID.test(id)||![0,1].includes(direction))fail('RELEASE_LINEAGE_LOOKUP_INVALID');
  const cache=identityCache(tree).neighbors,cacheKey=id+':'+direction;if(cache.has(cacheKey))return structuredClone(cache.get(cacheKey));
  const prefix=await releaseDigest(id)+String(direction),rows=[];let after=null;
  do{
    const page=await tree.scan('lineage',{prefix,after,limit:50});
    for(const item of page.rows){
      const r=await validateLineageAdjacency(tree,item,id,direction);
      rows.push(r);if(rows.length>4096)fail('ADMIN_LINEAGE_GRAPH_BATCH_REQUIRED');
    }
    after=page.more?page.last_key:null;
  }while(after!==null);
  cache.set(cacheKey,rows);return structuredClone(rows);
}
export async function lineageTypeContext(tree,id,{projectionOverrides=null}={}){
  const cache=identityCache(tree).contexts;if(!projectionOverrides&&cache.has(id))return structuredClone(cache.get(id));
  const group=async member=>projectionOverrides?.get(member)??await canonicalType(tree,member),start=await group(id),refs=new Map();
  async function walk(direction){
    const queue=[start],groups=new Map(),edges=new Map();
    while(queue.length){
      const g=queue.shift();if(groups.has(g.canonical_id))continue;groups.set(g.canonical_id,g);edges.set(g.canonical_id,new Set());
      if(groups.size>256)fail('ADMIN_LINEAGE_GRAPH_BATCH_REQUIRED');
      for(const member of g.member_ids)for(const r of await lineageNeighbors(tree,member,direction)){
        for(const ref of r.record_refs){if(refs.has(ref.id)&&refs.get(ref.id)!==ref.hash)fail('RELEASE_LINEAGE_RECORD_CHANGED');refs.set(ref.id,ref.hash);}
        const other=await group(direction===0?r.child_id:r.parent_id);
        if(other.canonical_id===g.canonical_id)fail('RELEASE_LINEAGE_CYCLE');
        edges.get(g.canonical_id).add(other.canonical_id);queue.push(other);
      }
    }
    // Prove this reachable graph is acyclic even under a proposed alias merge.
    const degree=new Map([...groups.keys()].map(cid=>[cid,0]));for(const children of edges.values())for(const cid of children)degree.set(cid,degree.get(cid)+1);
    const ready=[...degree].filter(([,n])=>n===0).map(([cid])=>cid);let visited=0;
    while(ready.length){const cid=ready.pop();visited++;for(const child of edges.get(cid)??[]){degree.set(child,degree.get(child)-1);if(degree.get(child)===0)ready.push(child);}}
    if(visited!==groups.size)fail('RELEASE_LINEAGE_CYCLE');
    return [...groups.values()].filter(g=>g.canonical_id!==start.canonical_id).flatMap(g=>g.member_ids);
  }
  const ancestor_ids=await walk(1),descendant_ids=await walk(0);
  const result={canonical_id:start.canonical_id,member_ids:start.member_ids,ancestor_ids:[...new Set([...start.ancestor_ids,...ancestor_ids])].sort(),descendant_ids:[...new Set(descendant_ids)].sort(),lineage_refs:[...refs].sort(([a],[b])=>a.localeCompare(b)).map(([id,hash])=>({id,hash}))};
  if(!projectionOverrides)cache.set(id,result);return structuredClone(result);
}
export async function selectedTypeHistory(tree, ids, { issueId, at, validateEntry } = {}) {
  const published = new Set(), queued = new Set(), canonical = new Map(), components = new Set(),contexts=[];
  for (const id of ids) {
    const alias = await lineageTypeContext(tree,id), members = [...new Set([...alias.member_ids, ...alias.ancestor_ids])],holdMembers=[...new Set([...members,...alias.descendant_ids])];
    if (components.has(alias.canonical_id)) fail('RELEASE_DUPLICATE_FUNCTIONAL_TYPE'); components.add(alias.canonical_id); canonical.set(id, alias.canonical_id);
    if(contexts.some(other=>other.member_ids.some(cid=>alias.ancestor_ids.includes(cid))||alias.member_ids.some(cid=>other.ancestor_ids.includes(cid))))fail('RELEASE_DUPLICATE_FUNCTIONAL_TYPE');contexts.push(alias);
    for (const member of members) {
      const type = await tree.get('types', await releaseDigest(member));
      if (type) {
        if (type.concept_id !== member || type.first_exposed_at !== null && releaseTime(type.first_exposed_at) > releaseTime(at)) fail('RELEASE_TYPE_PROJECTION_INVALID');
        if (type.first_exposed_at !== null && type.first_publication?.issue_id !== issueId) published.add(id);
      }
    }
    for(const member of holdMembers){
      const holds = await tree.scan('holds', { prefix: await releaseDigest(member), limit: 50 });
      if (holds.more) fail('RELEASE_SELECTED_HOLD_PAGINATION_REQUIRED');
      for (const item of holds.rows) {
        const hold = item.value, entry = await archiveEntry(tree, hold.sequence, validateEntry, at);
        if (hold.concept_id !== member || hold.issue_id !== entry.issue_id || await releaseHoldKey(member, hold.issue_id) !== item.key ||
            !Array.isArray(entry.recommendations) || !entry.recommendations.some(row => row.concept_id === member && row.status === hold.status)) fail('RELEASE_HOLD_BINDING_INVALID');
        if (firstAvailability(entry, at) !== null && hold.issue_id !== issueId && members.includes(member)) published.add(id);
        else if (firstAvailability(entry, at)===null && entry.remote_state === 'active' && hold.issue_id !== issueId) queued.add(id);
      }
    }
  }
  return { published, queued, canonical };
}
