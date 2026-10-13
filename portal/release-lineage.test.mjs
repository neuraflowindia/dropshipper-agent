// Manufactured immutable-page graph fixtures. No provider or production writes.
import test from 'node:test';
import assert from 'node:assert/strict';
import {ReleaseTree,emptyReleaseRoot,releaseBytes,releaseDigest,validateReleaseRoot} from './release-pages.mjs';
import {lineageKey,lineageTypeContext} from './release-history.mjs';
const CLIENT='lineage-client',AT='2026-10-09T10:00:00.000Z';
function store(){const objects=new Map();return {objects,async get(key){const b=objects.get(key);return b&&{size:b.length,arrayBuffer:async()=>b.slice().buffer};},async put(key,bytes){if(!objects.has(key))objects.set(key,bytes.slice());return {etag:'synthetic'};}};}
async function pair(tree,parent,child){const r={parent_id:parent,child_id:child,record_refs:[{id:'functional-lineage-'+(await releaseDigest([parent,child])).slice(0,40),hash:await releaseDigest([parent,child])}]};await tree.set('lineage',await lineageKey(parent,child),r);await tree.set('lineage',await lineageKey(parent,child,1),r);}

test('old eight-kind roots stay byte-identical until explicit lineage write',async()=>{
  const root=emptyReleaseRoot(CLIENT,AT),bytes=releaseBytes(root),tree=new ReleaseTree(store(),root);
  assert.equal(Object.keys(root.pages).length,8);assert.equal(await tree.get('lineage',await lineageKey('a','b')),null);assert.deepEqual((await tree.scan('lineage')).rows,[]);assert.deepEqual(releaseBytes(root),bytes);
  await pair(tree,'a','b');validateReleaseRoot(root,CLIENT,AT);assert.equal(Object.keys(root.pages).length,9);assert.equal(root.pages.lineage.count,2);
});

test('actual direct adjacency gives transitive ancestry in either insertion order without joining siblings',async()=>{
  for(const pairs of [[['a','b'],['b','c'],['a','sibling']],[['b','c'],['a','sibling'],['a','b']]]){
    const bucket=store(),root=emptyReleaseRoot(CLIENT,AT),tree=new ReleaseTree(bucket,root);for(const [parent,child]of pairs)await pair(tree,parent,child);await tree.persist();
    const c=await lineageTypeContext(new ReleaseTree(bucket,root),'c');assert.deepEqual(c.ancestor_ids,['a','b']);assert.deepEqual(c.descendant_ids,[]);assert.equal(c.lineage_refs.length,2);
    const b=await lineageTypeContext(new ReleaseTree(bucket,root),'b');assert.deepEqual(b.ancestor_ids,['a']);assert.deepEqual(b.descendant_ids,['c']);assert.ok(!b.ancestor_ids.includes('sibling')&&!b.descendant_ids.includes('sibling'));
    assert.deepEqual((await lineageTypeContext(new ReleaseTree(bucket,root),'a')).descendant_ids,['b','c','sibling']);
  }
});

test('cycles, one-sided pointers, changed hashes and bounded graph overflow cannot produce a context',async()=>{
  const root=emptyReleaseRoot(CLIENT,AT),tree=new ReleaseTree(store(),root);await pair(tree,'a','b');await pair(tree,'b','a');await assert.rejects(lineageTypeContext(tree,'a'),/LINEAGE_CYCLE/);
  const healthy=new ReleaseTree(store(),emptyReleaseRoot(CLIENT,AT));await pair(healthy,'a','b');await healthy.set('lineage',await lineageKey('a','b',1),null);await assert.rejects(lineageTypeContext(healthy,'a'),/LINEAGE_MIRROR_INVALID/);
  const bounded=new ReleaseTree(store(),emptyReleaseRoot(CLIENT,AT),{writeLimit:2000});for(let i=0;i<257;i++)await pair(bounded,'hub','child-'+i);await assert.rejects(lineageTypeContext(bounded,'hub'),/LINEAGE_GRAPH_BATCH_REQUIRED|READ_BUDGET/);
});

test('proposed equivalence merges preserve directional graph and reject collapsed ancestor cycles',async()=>{
  const tree=new ReleaseTree(store(),emptyReleaseRoot(CLIENT,AT));await pair(tree,'a','b');await pair(tree,'a','sibling');
  const grouped={canonical_id:'b',member_ids:['b','sibling'],ancestor_ids:[]},siblings=new Map([['b',grouped],['sibling',grouped]]);
  assert.deepEqual((await lineageTypeContext(tree,'b',{projectionOverrides:siblings})).ancestor_ids,['a']);
  const bad={canonical_id:'a',member_ids:['a','b'],ancestor_ids:[]};await assert.rejects(lineageTypeContext(tree,'a',{projectionOverrides:new Map([['a',bad],['b',bad]])}),/LINEAGE_CYCLE/);
});

test('2,000 direct relationship rows remain pageable without loading their full graph',async()=>{
  const bucket=store(),root=emptyReleaseRoot(CLIENT,AT),writer=new ReleaseTree(bucket,root,{writeLimit:2000}),entries=[];
  for(let i=0;i<2000;i++){const child='descendant-'+i,value={parent_id:'parent',child_id:child,record_refs:[{id:'functional-lineage-'+(await releaseDigest(child)).slice(0,40),hash:await releaseDigest(child)}]};entries.push({key:await lineageKey('parent',child),value});}
  entries.sort((a,b)=>a.key.localeCompare(b.key));root.pages.lineage=await writer.build('lineage',entries);await writer.persist();validateReleaseRoot(root,CLIENT,AT);
  const prefix=await releaseDigest('parent')+'0',seen=new Set();let after=null,pages=0;
  // A shared endpoint has 65 fixed prefix digits. The existing uncompressed
  // 16-way trie may therefore read that path plus its small selected leaf set;
  // this stays below the real owner-read budget (160), without changing it.
  do{const reader=new ReleaseTree(bucket,root,{readLimit:96}),page=await reader.scan('lineage',{prefix,after,limit:20});assert.ok(reader.reads<=96);pages++;for(const row of page.rows){assert.ok(!seen.has(row.key));seen.add(row.key);}after=page.more?page.last_key:null;}while(after);
  assert.equal(seen.size,2000);assert.equal(pages,100);assert.ok(releaseBytes(root).length<2000);
});

test('cached graph and alias results cannot survive a changed immutable root descriptor',async()=>{
  const tree=new ReleaseTree(store(),emptyReleaseRoot(CLIENT,AT));await pair(tree,'a','b');assert.deepEqual((await lineageTypeContext(tree,'b')).ancestor_ids,['a']);await pair(tree,'b','c');assert.deepEqual((await lineageTypeContext(tree,'c')).ancestor_ids,['a','b']);
  const context=await lineageTypeContext(tree,'b');context.ancestor_ids.length=0;assert.deepEqual((await lineageTypeContext(tree,'b')).ancestor_ids,['a']);
  await tree.set('aliases',await releaseDigest('a'),{member_id:'a',canonical_id:'a',member_ids:['a','other'],record_refs:[],ancestor_ids:[]});await assert.rejects(lineageTypeContext(tree,'b'),/ALIAS_PROJECTION_INVALID/);
});
