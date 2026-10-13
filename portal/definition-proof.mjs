// Optional B-definition acceptance is a saved original, not a caller assertion.
// Nodes retain Python's original decimal and large-integer JSON spellings.
import { ReleasePageError, releaseCanonical, releaseDigest, releaseHash } from './release-pages.mjs';

const enc = new TextEncoder(), ID = /^[A-Za-z0-9_-]{1,80}$/, HASH = /^[a-f0-9]{64}$/;
const MEANING = ['primary_use','mechanism','use_context','compatibility_constraints','included_distinctions','excluded_distinctions'];
const DEFINITION = ['report_title',...MEANING];
const DESCRIPTION = ['label','category','function','mechanism','audience','features','basis'];
const CATEGORIES = ['Home and Decor','Baby Products','Home and Kitchen','Travel and Outdoor'];
const WS = /[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/u;
const object = v => v !== null && typeof v === 'object' && !Array.isArray(v);
const exact = (v,fields) => object(v) && Object.keys(v).sort().join('|') === [...fields].sort().join('|');
const omit = (v,fields) => Object.fromEntries(Object.entries(v).filter(([k])=>!fields.includes(k)));
const same = (a,b) => releaseCanonical(a) === releaseCanonical(b);
const order = (a,b) => { const x=Array.from(a,c=>c.codePointAt(0)),y=Array.from(b,c=>c.codePointAt(0));for(let i=0;i<Math.min(x.length,y.length);i++)if(x[i]!==y[i])return x[i]-y[i];return x.length-y.length; };
const sortUnique = v => [...new Set(v)].sort(order);
const hashRaw = n => releaseHash(enc.encode(n.raw));
const key = async (prefix,value) => prefix+'-'+(await releaseDigest(value)).slice(0,40);

export async function validateDefinitionAcceptance(recordNode,{asOf=null,fail=(code,status=409)=>{throw new ReleasePageError(code,status);}}={}) {
  const check = (condition,code='ADMIN_DEFINITION_ACCEPTANCE_INVALID') => { if(!condition)fail(code,409); };
  const r=recordNode.value, outer=r?.job_proof;
  if(!object(outer)||!Object.hasOwn(outer,'definition_acceptance'))return null;
  const node=recordNode.children.job_proof?.children.definition_acceptance,p=node?.value;
  check(exact(outer,['request_hash','result_hash','definition_acceptance'])&&exact(p,['contract','job_id','original_job','attempt','result_fingerprint','item','binding','request_hash','binding_key','result_receipt','result_hash','accepted_item','accepted_item_hash'])&&p.contract==='functional-definition-original-acceptance-v1');
  check(enc.encode(node.raw).length<=480*1024&&r.job_id===p.job_id&&outer.request_hash===p.request_hash&&outer.result_hash===p.result_hash);
  const text = value => { check(typeof value==='string'&&value.length>0&&value.length<=2000&&value.split(WS).filter(Boolean).length>0);return value; };
  const clean = value => text(value).split(WS).filter(Boolean).join(' ');
  const date = value => {
    check(typeof value==='string'&&/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value));
    const at=Date.parse(value);check(Number.isFinite(at)&&new Date(at).toISOString().slice(0,19)===value.slice(0,19));return at;
  };
  const definition = value => {
    check(exact(value,DEFINITION));const d={};
    for(const f of DEFINITION.slice(0,3))d[f]=clean(value[f]);
    for(const f of MEANING.slice(2)){check(Array.isArray(value[f])&&value[f].length<=40);d[f]=sortUnique(value[f].map(clean));}
    check(!d.included_distinctions.some(v=>d.excluded_distinctions.includes(v))&&enc.encode(releaseCanonical(d)).length<=32*1024);return d;
  };
  const item = value => {
    check(exact(value,['id','category','examples'])&&ID.test(value.id??'')&&CATEGORIES.includes(value.category)&&Array.isArray(value.examples)&&value.examples.length>=1&&value.examples.length<=20);
    for(const e of value.examples){
      check(exact(e,['child_id','semantic_id','description'])&&ID.test(e.child_id??'')&&ID.test(e.semantic_id??''));const d=e.description;
      check(exact(d,DESCRIPTION)&&[...CATEGORIES,'Out of scope'].includes(d.category)&&['observed','inferred'].includes(d.basis)&&Array.isArray(d.features)&&d.features.length<=40);
      for(const f of ['label','function','mechanism','audience'])text(d[f]);d.features.forEach(text);
    }
    check(same(value.examples.map(e=>e.child_id),sortUnique(value.examples.map(e=>e.child_id))));return value;
  };
  const selected=item(p.item),b=p.binding;
  check(exact(b,['concept_id','expected_meaning_hash','item_hash','children','prepared_at'])&&b.concept_id===selected.id&&b.item_hash===await releaseDigest(selected)&&HASH.test(b.expected_meaning_hash??'')&&exact(b.children,selected.examples.map(e=>e.child_id)));
  date(b.prepared_at);
  for(const [cid,child]of Object.entries(b.children))check(exact(child,['semantic_id','association_hash','description_hash','description_record_hash','observation_id','observation_hash'])&&ID.test(child.semantic_id??'')&&child.semantic_id===selected.examples.find(e=>e.child_id===cid).semantic_id&&ID.test(child.observation_id??'')&&['association_hash','description_hash','description_record_hash','observation_hash'].every(f=>HASH.test(child[f]??'')));
  const j=p.original_job,request=j?.request,requestNode=node.children.original_job?.children.request;
  check(object(j)&&exact(request,['role','payload','model','prompt_version','runner'])&&request.role==='B'&&request.model==='gpt-6-luna'&&request.prompt_version==='research-b-functional-definition-v1'&&typeof request.runner==='string'&&request.runner.length>0&&p.job_id===await key('job',request));
  const payload=request.payload;
  check(exact(payload,['mode','definition_contract','items'])&&payload.mode==='functional_definition_review'&&payload.definition_contract==='functional-definition-review-v1'&&Array.isArray(payload.items)&&payload.items.length>=1&&payload.items.length<=5&&enc.encode(releaseCanonical(payload)).length<=48*1024);
  const bindings={};
  for(const candidate of payload.items){item(candidate);check(!Object.hasOwn(bindings,candidate.id));bindings[candidate.id]=await releaseDigest(['B',candidate,omit(payload,['items']),request.model,request.prompt_version,request.runner]);}
  check(same(j.binding_keys,bindings)&&same(payload.items.filter(v=>v.id===selected.id),[selected])&&p.binding_key===bindings[selected.id]&&p.request_hash===await hashRaw(requestNode),'ADMIN_DEFINITION_ORIGINAL_JOB_CHANGED');
  check(typeof p.attempt==='string'&&/^(?:initial|retry-(?:[1-9]|1[0-9]|20))$/.test(p.attempt)&&Array.isArray(p.result_fingerprint)&&p.result_fingerprint.length===5);
  // Filesystem nanoseconds exceed JavaScript's exact integer range. Validate
  // original integer tokens, never round them or change the bound record hash.
  for(const n of Object.values(node.children.result_fingerprint.children))check(/^(?:0|[1-9]\d*)$/.test(n.raw));
  const receipt=p.result_receipt;
  check(exact(receipt,['request_hash','response','usage','completed_at'])&&receipt.request_hash===p.request_hash&&p.result_hash===await hashRaw(node.children.result_receipt),'ADMIN_DEFINITION_ORIGINAL_RESULT_CHANGED');
  const completed=date(receipt.completed_at);check(date(r.reviewed_at)>=completed);if(asOf!==null)check(completed<=date(asOf),'ADMIN_DEFINITION_RESULT_FUTURE');
  check(exact(receipt.response,['results'])&&Array.isArray(receipt.response.results)&&receipt.response.results.length<=100);
  const rows=receipt.response.results.filter(v=>v?.id===selected.id);check(rows.length===1);const answer=rows[0];
  check(exact(answer,['id','decision','definition','references','example_decisions','reason'])&&answer.id===selected.id&&['prepared','unclear'].includes(answer.decision)&&Array.isArray(answer.references)&&Array.isArray(answer.example_decisions));text(answer.reason);
  const known=new Set(selected.examples.map(e=>e.child_id)),decisions={};
  for(const row of answer.example_decisions){check(exact(row,['child_id','decision','reason'])&&known.has(row.child_id)&&!Object.hasOwn(decisions,row.child_id)&&['same','different','unclear'].includes(row.decision));text(row.reason);decisions[row.child_id]=row.decision;}
  check(same(Object.keys(decisions).sort(order),[...known].sort(order))&&answer.references.every(ref=>typeof ref==='string'&&known.has(ref))&&new Set(answer.references).size===answer.references.length);
  let d=null;
  if(answer.decision==='prepared'){d=definition(answer.definition);check(d.report_title.length<=160&&answer.references.length>0&&answer.references.every(ref=>decisions[ref]==='same'));}
  else check(answer.definition===null&&answer.references.length===0&&!Object.values(decisions).includes('same'));
  const accepted={...answer,definition:d,references:[...answer.references].sort(order),example_decisions:[...answer.example_decisions].sort((a,b)=>order(a.child_id,b.child_id))};
  check(enc.encode(releaseCanonical(accepted)).length<=48*1024&&same(accepted,p.accepted_item)&&p.accepted_item_hash===await releaseDigest(accepted),'ADMIN_DEFINITION_ACCEPTED_RESULT_CHANGED');
  const expectedDecisions=Object.fromEntries(accepted.example_decisions.map(row=>[row.child_id,{decision:row.decision,reason:clean(row.reason),association_hash:b.children[row.child_id].association_hash,description_hash:b.children[row.child_id].description_hash}]));
  const basis=Object.fromEntries(accepted.references.map(child=>[b.children[child].observation_id,b.children[child].observation_hash]));
  check(accepted.decision==='prepared'&&r.concept_id===selected.id&&same(definition(r.definition),accepted.definition)&&same(r.example_decisions,expectedDecisions)&&same(r.basis_refs,basis),'ADMIN_DEFINITION_ACCEPTED_RESULT_CHANGED');
  return accepted;
}
