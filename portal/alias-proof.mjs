// Portable original judgments, not caller-supplied alias projection rows.
// Only the authenticated owner bridge may submit this proof. Hashes bind the
// actual saved input/result; they are not signatures or proof of omitted facts.
import { originalNodes } from './functional-packet.mjs';
import { validateDefinitionAcceptance } from './definition-proof.mjs';
import { ReleasePageError, releaseCanonical, releaseDigest, releaseHash } from './release-pages.mjs';

const enc = new TextEncoder(), ID = /^[A-Za-z0-9_-]{1,80}$/, HASH = /^[a-f0-9]{64}$/;
const MEANING = ['primary_use', 'mechanism', 'use_context', 'compatibility_constraints', 'included_distinctions', 'excluded_distinctions'];
const DEFINITION = ['report_title', ...MEANING];
const INTENT = ['kind','concept_id','definition','meaning_hash','presentation_hash','basis_refs','reviewer','reviewed_at','job_id','job_proof','example_decisions','accepted'];
const CATEGORIES = ['Home and Decor','Baby Products','Home and Kitchen','Travel and Outdoor'];
const fail = (code = 'ADMIN_ALIAS_PROOF_INVALID') => { throw new ReleasePageError(code, 409); };
const check = (value, code) => { if (!value) fail(code); };
const object = v => v !== null && typeof v === 'object' && !Array.isArray(v);
const exact = (v, fields) => object(v) && Object.keys(v).sort().join('|') === [...fields].sort().join('|');
const same = (a,b) => releaseCanonical(a) === releaseCanonical(b);
const omit = (v,fields) => Object.fromEntries(Object.entries(v).filter(([k]) => !fields.includes(k)));
const pick = (v,fields) => Object.fromEntries(fields.map(k => [k,v[k]]));
const integer = (n,min=0) => Number.isSafeInteger(n) && n >= min;
const codepointOrder = (a,b) => { const x=Array.from(a,c=>c.codePointAt(0)),y=Array.from(b,c=>c.codePointAt(0)); for(let i=0;i<Math.min(x.length,y.length);i++)if(x[i]!==y[i])return x[i]-y[i]; return x.length-y.length; };
const ordered = rows => Array.isArray(rows) && rows.every(v=>typeof v==='string') && same(rows,[...new Set(rows)].sort(codepointOrder));
const cleanText = v => typeof v === 'string' && v.length > 0 && v.length <= 2000 && v === v.split(/[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/u).filter(Boolean).join(' ');
function date(value) {
  check(typeof value==='string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value));
  const at=Date.parse(value); check(Number.isFinite(at)&&new Date(at).toISOString().slice(0,19)===value.slice(0,19)); return at;
}
const key = async (prefix,value) => prefix+'-'+(await releaseDigest(value)).slice(0,40);
const rawObject = (node, excluded=[],included=null) => '{'+Object.keys(node.children).filter(k=>!excluded.includes(k)&&(included===null||included.includes(k))).sort(codepointOrder).map(k=>releaseCanonical(k)+':'+node.children[k].raw).join(',')+'}';
const rawHash = (node,excluded=[]) => releaseHash(enc.encode(rawObject(node,excluded)));
async function original(wrapper) {
  check(exact(wrapper,['contract','canonical_json','sha256'])&&wrapper.contract==='frozen-original-json-v1'&&HASH.test(wrapper.sha256??'')&&typeof wrapper.canonical_json==='string'&&enc.encode(wrapper.canonical_json).length<=512*1024);
  check(await releaseHash(enc.encode(wrapper.canonical_json))===wrapper.sha256,'ADMIN_ALIAS_ORIGINAL_CHANGED');
  const n=originalNodes(wrapper.canonical_json,fail); n.hash=wrapper.sha256; return n;
}
function definitionValue(d) {
  check(exact(d,DEFINITION)&&['report_title','primary_use','mechanism'].every(k=>cleanText(d[k])));
  for(const f of MEANING.slice(2))check(ordered(d[f])&&d[f].length<=40&&d[f].every(cleanText));
  check(!d.included_distinctions.some(v=>d.excluded_distinctions.includes(v))&&enc.encode(releaseCanonical(d)).length<=32*1024);
}
async function definition(n,id,asOf) {
  const r=n.value;
  check(ID.test(id)&&exact(r,[...INTENT,'revision','previous_definition_id','previous_meaning_hash','record_hash'])&&r.kind==='functional-definition-v1'&&r.accepted===true&&ID.test(r.concept_id??'')&&integer(r.revision,1)&&cleanText(r.reviewer));
  date(r.reviewed_at); definitionValue(r.definition);
  check(object(r.basis_refs)&&Object.keys(r.basis_refs).length>0&&Object.entries(r.basis_refs).every(([id,h])=>ID.test(id)&&HASH.test(h??''))&&object(r.example_decisions));
  check(r.record_hash===await rawHash(n,['record_hash'])&&r.meaning_hash===await releaseDigest(pick(r.definition,MEANING))&&r.presentation_hash===await releaseDigest({report_title:r.definition.report_title})&&id==='functional-definition-'+(await releaseHash(enc.encode(rawObject(n,[],INTENT)))).slice(0,40),'ADMIN_ALIAS_DEFINITION_CHANGED');
  await validateDefinitionAcceptance(n,{asOf,fail});
  return r;
}
async function input(value,definitions,at) {
  check(exact(value,['contract','member_ids','definition_bindings','category','reviewed_at','input_hash'])&&value.contract==='functional-alias-input-v1'&&value.input_hash===await releaseDigest(omit(value,['input_hash'])));
  check(ordered(value.member_ids)&&value.member_ids.length>=2&&value.member_ids.length<=20&&value.member_ids.every(v=>ID.test(v))&&exact(value.definition_bindings,value.member_ids)&&CATEGORIES.includes(value.category)&&date(value.reviewed_at)<=at);
  for(const cid of value.member_ids){const b=value.definition_bindings[cid],n=definitions[b?.definition_id];
    check(exact(b,['definition_id','record_hash','meaning_hash'])&&n&&b.record_hash===n.hash&&n.value.concept_id===cid&&n.value.meaning_hash===b.meaning_hash&&date(n.value.reviewed_at)<=date(value.reviewed_at),'ADMIN_ALIAS_DEFINITION_CHANGED');}
  const members=value.member_ids.map(cid=>{const b=value.definition_bindings[cid];return {concept_id:cid,meaning_hash:b.meaning_hash,definition:pick(definitions[b.definition_id].value.definition,MEANING)};});
  const item={category:value.category,members}; item.id=await key('functional-alias-review',item); return item;
}
async function jobProof(prepared,item,node,at) {
  const p=node?.value;
  check(exact(p,['contract','job_id','item_id','job_hash','original_job','request_hash','binding_key','result_receipt','result_hash','accepted_item','accepted_item_hash'])&&p.contract==='functional-alias-job-proof-v1');
  const j=p.original_job,r=j?.request,requestNode=node.children.original_job?.children.request;
  check(object(j)&&exact(r,['role','payload','model','prompt_version','runner'])&&r.role==='B'&&r.prompt_version==='research-b-functional-alias-v1'&&typeof r.model==='string'&&r.model.length>0&&typeof r.runner==='string'&&r.runner.length>0&&p.job_id===await key('job',r));
  const payload=r.payload;
  check(exact(payload,['mode','alias_contract','items'])&&payload.mode==='functional_alias_review'&&payload.alias_contract==='functional-alias-review-v1'&&Array.isArray(payload.items)&&payload.items.length>=1&&payload.items.length<=10);
  const bindings={};
  for(const candidate of payload.items){
    check(exact(candidate,['id','category','members'])&&CATEGORIES.includes(candidate.category)&&Array.isArray(candidate.members)&&candidate.members.length>=2&&candidate.members.length<=20);
    check(ordered(candidate.members.map(v=>v.concept_id))&&candidate.members.every(v=>ID.test(v.concept_id??'')));
    for(const m of candidate.members){check(exact(m,['concept_id','meaning_hash','definition'])&&exact(m.definition,MEANING));definitionValue({report_title:'Accepted functional type',...m.definition});check(m.meaning_hash===await releaseDigest(m.definition));}
    check(candidate.id===await key('functional-alias-review',omit(candidate,['id']))&&!Object.hasOwn(bindings,candidate.id));
    bindings[candidate.id]=await releaseDigest(['B',candidate,omit(payload,['items']),r.model,r.prompt_version,r.runner]);
  }
  check(same(j.binding_keys,bindings)&&same(payload.items.find(v=>v.id===item.id),item)&&p.item_id===item.id&&p.binding_key===bindings[item.id]&&p.request_hash===await releaseHash(enc.encode(requestNode.raw))&&p.job_hash===await releaseHash(enc.encode(node.children.original_job.raw)),'ADMIN_ALIAS_JOB_CHANGED');
  const receipt=p.result_receipt;
  check(exact(receipt,['request_hash','response','usage','completed_at'])&&receipt.request_hash===p.request_hash&&date(receipt.completed_at)<=at&&p.result_hash===await releaseHash(enc.encode(node.children.result_receipt.raw))&&exact(receipt.response,['results'])&&Array.isArray(receipt.response.results));
  const rows=receipt.response.results.filter(v=>v?.id===item.id);
  check(rows.length===1&&exact(rows[0],['id','decision','reason'])&&['same','different','unclear'].includes(rows[0].decision)&&typeof rows[0].reason==='string'&&rows[0].reason.trim()&&rows[0].reason.length<=2000);
  check(same(rows[0],p.accepted_item)&&p.accepted_item_hash===await releaseDigest(rows[0]),'ADMIN_ALIAS_JOB_RESULT_CHANGED');
  return p.accepted_item;
}

export { original as readFunctionalOriginal, input as validateFunctionalDefinitionInput };
export async function readFunctionalDefinitions(wrappers,{asOf}={}) {
  check(object(wrappers));const at=date(asOf),definitions={},groups=new Map();
  for(const [id,w] of Object.entries(wrappers)){const n=await original(w);await definition(n,id,asOf);check(date(n.value.reviewed_at)<=at,'ADMIN_ALIAS_DEFINITION_FUTURE');definitions[id]=n;const r=n.value;if(!groups.has(r.concept_id))groups.set(r.concept_id,[]);groups.get(r.concept_id).push([id,r]);}
  for(const rows of groups.values()){rows.sort((a,b)=>a[1].revision-b[1].revision);for(const [i,[id,r]] of rows.entries())check(r.revision===i+1&&r.previous_definition_id===(i?rows[i-1][0]:null)&&(i?r.previous_meaning_hash===rows[i-1][1].meaning_hash:HASH.test(r.previous_meaning_hash??'')),'ADMIN_ALIAS_DEFINITION_CHAIN_CHANGED');}
  return {definitions,groups,latestDefinitions:Object.fromEntries([...groups].map(([cid,rows])=>[cid,rows.at(-1)[1]]))};
}
export async function validateAliasProof(proof,{asOf}={}) {
  check(exact(proof,['contract','records','definitions','reviews'])&&proof.contract==='functional-alias-portable-v1'&&object(proof.records)&&object(proof.definitions)&&object(proof.reviews));
  check(Object.keys(proof.records).length>=1&&Object.keys(proof.records).length<=20&&Object.keys(proof.reviews).length<=100);
  const at=date(asOf),{definitions,groups,latestDefinitions}=await readFunctionalDefinitions(proof.definitions,{asOf}),records={},reviews={},used=new Set();
  for(const [id,w] of Object.entries(proof.records)){
    const n=await original(w),r=n.value;
    check(exact(r,['kind','input','method','decision','reviewer','reason','job_proof','record_hash'])&&r.kind==='functional-alias-v1'&&r.decision==='same'&&['exact_meaning','operator','model'].includes(r.method)&&typeof r.reviewer==='string'&&r.reviewer.trim()&&r.reviewer.length<=2000&&typeof r.reason==='string'&&r.reason.trim()&&r.reason.length<=2000);
    check(r.record_hash===await rawHash(n,['record_hash'])&&id==='functional-alias-'+(await rawHash(n,['record_hash'])).slice(0,40),'ADMIN_ALIAS_RECORD_CHANGED');
    const item=await input(r.input,definitions,at);r.input.member_ids.forEach(cid=>used.add(cid));
    if(r.method==='model'){const result=await jobProof(r.input,item,n.children.job_proof,at);check(result.decision==='same'&&result.reason===r.reason,'ADMIN_ALIAS_JOB_RESULT_CHANGED');}
    else check(r.job_proof===null&&(r.method!=='exact_meaning'||new Set(item.members.map(v=>v.meaning_hash)).size===1),'ADMIN_ALIAS_EXACT_MEANING_REQUIRED');
    records[id]={row:r,hash:n.hash,wrapper:w};
  }
  for(const [id,w] of Object.entries(proof.reviews)){
    const n=await original(w),r=n.value;
    check(exact(r,['kind','input','item_id','method','result','job_proof','record_hash'])&&r.kind==='functional-alias-review-v1'&&r.method==='model'&&r.record_hash===await rawHash(n,['record_hash'])&&id==='functional-alias-semantic-review-'+(await rawHash(n,['record_hash'])).slice(0,40),'ADMIN_ALIAS_REVIEW_CHANGED');
    const item=await input(r.input,definitions,at),answer=await jobProof(r.input,item,n.children.job_proof,at);
    check(r.item_id===answer.id&&same(r.result,answer));r.input.member_ids.forEach(cid=>used.add(cid));reviews[id]={row:r,hash:n.hash};
  }
  check([...groups.keys()].every(cid=>used.has(cid)),'ADMIN_ALIAS_UNUSED_DEFINITION');
  return {records,reviews,definitions,latestDefinitions,proof_hash:await releaseDigest(proof)};
}
