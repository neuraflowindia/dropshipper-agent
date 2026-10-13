// An additional example is admitted only by its actual saved B answer. The
// accepted type stays unchanged, and original JSON numbers retain their bytes.
import { ReleasePageError, releaseCanonical, releaseDigest, releaseHash } from './release-pages.mjs';

const enc=new TextEncoder(),ID=/^[A-Za-z0-9_-]{1,80}$/,HASH=/^[a-f0-9]{64}$/;
const MEANING=['primary_use','mechanism','use_context','compatibility_constraints','included_distinctions','excluded_distinctions'];
const CATEGORIES=['Home and Decor','Baby Products','Home and Kitchen','Travel and Outdoor'];
const WS=/[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/u;
const object=v=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const exact=(v,fields)=>object(v)&&Object.keys(v).sort().join('|')===[...fields].sort().join('|');
const omit=(v,fields)=>Object.fromEntries(Object.entries(v).filter(([k])=>!fields.includes(k)));
const same=(a,b)=>releaseCanonical(a)===releaseCanonical(b);
const order=(a,b)=>{const x=Array.from(a,c=>c.codePointAt(0)),y=Array.from(b,c=>c.codePointAt(0));for(let i=0;i<Math.min(x.length,y.length);i++)if(x[i]!==y[i])return x[i]-y[i];return x.length-y.length;};
const sortUnique=v=>[...new Set(v)].sort(order);
const key=async(prefix,value)=>prefix+'-'+(await releaseDigest(value)).slice(0,40);
const rawHash=n=>releaseHash(enc.encode(n.raw));
const rawObject=(node,excluded=[])=>'{'+Object.keys(node.children).filter(k=>!excluded.includes(k)).sort(order).map(k=>JSON.stringify(k)+':'+node.children[k].raw).join(',')+'}';

export async function validateExampleReview(node,recordId,{asOf=null,fail=(code,status=409)=>{throw new ReleasePageError(code,status);}}={}){
  const check=(condition,code='ADMIN_EXAMPLE_REVIEW_INVALID')=>{if(!condition)fail(code,409);};
  const text=v=>{check(typeof v==='string'&&v.length>0&&v.length<=2000&&v.split(WS).filter(Boolean).length>0);return v;};
  const date=v=>{check(typeof v==='string'&&/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(v));const at=Date.parse(v);check(Number.isFinite(at)&&new Date(at).toISOString().slice(0,19)===v.slice(0,19));return at;};
  const item=value=>{
    check(exact(value,['id','concept_id','meaning_hash','definition','category','examples'])&&ID.test(value.concept_id??'')&&HASH.test(value.meaning_hash??'')&&exact(value.definition,MEANING)&&CATEGORIES.includes(value.category)&&Array.isArray(value.examples)&&value.examples.length>=1&&value.examples.length<=20);
    const d=value.definition;
    for(const f of MEANING.slice(0,2))check(text(d[f])===d[f].split(WS).filter(Boolean).join(' '));
    for(const f of MEANING.slice(2)){check(Array.isArray(d[f])&&d[f].length<=40);for(const v of d[f])check(text(v)===v.split(WS).filter(Boolean).join(' '));check(same(d[f],sortUnique(d[f])));}
    check(!d.included_distinctions.some(v=>d.excluded_distinctions.includes(v))&&enc.encode(releaseCanonical({...d,report_title:'Accepted functional type'})).length<=32*1024);
    for(const e of value.examples){const d=e.description;check(exact(e,['child_id','semantic_id','description'])&&ID.test(e.child_id??'')&&ID.test(e.semantic_id??'')&&exact(d,['label','category','function','mechanism','audience','features','basis'])&&[...CATEGORIES,'Out of scope'].includes(d.category)&&['observed','inferred'].includes(d.basis)&&Array.isArray(d.features)&&d.features.length<=40);for(const f of ['label','function','mechanism','audience'])text(d[f]);d.features.forEach(text);}
    check(same(value.examples.map(e=>e.child_id),sortUnique(value.examples.map(e=>e.child_id))));return value;
  };
  const r=node.value,pNode=node.children.job_proof,p=pNode?.value;
  check(exact(r,['kind','concept_id','meaning_hash','binding','job_id','job_proof','result','reviewed_at','record_hash'])&&r.kind==='functional-example-review-v1'&&r.record_hash===await releaseHash(enc.encode(rawObject(node,['record_hash']))));
  check(exact(p,['contract','job_id','original_job','attempt','result_fingerprint','item','binding','request_hash','binding_key','result_receipt','result_hash','accepted_item','accepted_item_hash'])&&p.contract==='functional-example-original-acceptance-v1'&&enc.encode(pNode.raw).length<=480*1024);
  const selected=item(p.item),b=p.binding;
  check(selected.meaning_hash===await releaseDigest(selected.definition)&&selected.id===await key('functional-example-question',omit(selected,['id'])));
  check(exact(b,['concept_id','meaning_hash','item_hash','definition_id','definition_hash','children','prepared_at'])&&b.concept_id===selected.concept_id&&b.meaning_hash===selected.meaning_hash&&b.item_hash===await releaseDigest(selected)&&ID.test(b.definition_id??'')&&HASH.test(b.definition_hash??'')&&object(b.children)&&Object.keys(b.children).length>0&&Object.keys(b.children).every(cid=>selected.examples.some(e=>e.child_id===cid)));date(b.prepared_at);
  for(const [cid,child]of Object.entries(b.children))check(exact(child,['semantic_id','association_hash','description_hash','description_record_hash','observation_id','observation_hash'])&&child.semantic_id===selected.examples.find(e=>e.child_id===cid).semantic_id&&ID.test(child.observation_id??'')&&['association_hash','description_hash','description_record_hash','observation_hash'].every(f=>HASH.test(child[f]??'')));
  const j=p.original_job,request=j?.request;
  check(object(j)&&exact(request,['role','payload','model','prompt_version','runner'])&&request.role==='B'&&request.model==='gpt-6-luna'&&request.prompt_version==='research-b-functional-example-v1'&&typeof request.runner==='string'&&request.runner.length>0&&p.job_id===await key('job',request));
  const payload=request.payload;check(exact(payload,['mode','example_contract','items'])&&payload.mode==='functional_example_review'&&payload.example_contract==='functional-example-review-v1'&&Array.isArray(payload.items)&&payload.items.length>=1&&payload.items.length<=5&&enc.encode(releaseCanonical(payload)).length<=48*1024);
  const bindings={},concepts=new Set();
  for(const candidate of payload.items){item(candidate);check(!concepts.has(candidate.concept_id)&&candidate.meaning_hash===await releaseDigest(candidate.definition)&&candidate.id===await key('functional-example-question',omit(candidate,['id'])));concepts.add(candidate.concept_id);bindings[candidate.id]=await releaseDigest(['B',candidate,omit(payload,['items']),request.model,request.prompt_version,request.runner]);}
  check(same(j.binding_keys,bindings)&&same(payload.items.filter(v=>v.id===selected.id),[selected])&&p.binding_key===bindings[selected.id]&&p.request_hash===await rawHash(pNode.children.original_job.children.request),'ADMIN_EXAMPLE_ORIGINAL_JOB_CHANGED');
  check(typeof p.attempt==='string'&&/^(?:initial|retry-(?:[1-9]|1[0-9]|20))$/.test(p.attempt)&&Array.isArray(p.result_fingerprint)&&p.result_fingerprint.length===5);for(const n of Object.values(pNode.children.result_fingerprint.children))check(/^(?:0|[1-9]\d*)$/.test(n.raw));
  const receipt=p.result_receipt;check(exact(receipt,['request_hash','response','usage','completed_at'])&&receipt.request_hash===p.request_hash&&p.result_hash===await rawHash(pNode.children.result_receipt),'ADMIN_EXAMPLE_ORIGINAL_RESULT_CHANGED');
  const completed=date(receipt.completed_at);check(date(r.reviewed_at)>=completed&&date(r.reviewed_at)>=date(b.prepared_at));if(asOf!==null)check(date(r.reviewed_at)<=date(asOf),'ADMIN_EXAMPLE_REVIEW_FUTURE');
  check(exact(receipt.response,['results'])&&Array.isArray(receipt.response.results)&&receipt.response.results.length<=100);const answers=receipt.response.results.filter(v=>v?.id===selected.id);check(answers.length===1);const answer=answers[0];
  check(exact(answer,['id','decisions','reason'])&&Array.isArray(answer.decisions));text(answer.reason);const known=new Set(selected.examples.map(e=>e.child_id)),decisions={};
  for(const row of answer.decisions){check(exact(row,['child_id','decision','reason'])&&known.has(row.child_id)&&!Object.hasOwn(decisions,row.child_id)&&['same','different','unclear'].includes(row.decision));text(row.reason);decisions[row.child_id]=row;}
  check(same(Object.keys(decisions).sort(order),[...known].sort(order)));const accepted={id:selected.id,decisions:Object.keys(decisions).sort(order).map(id=>decisions[id]),reason:answer.reason};
  check(enc.encode(releaseCanonical(accepted)).length<=48*1024&&same(accepted,p.accepted_item)&&p.accepted_item_hash===await releaseDigest(accepted)&&same(r.result,accepted)&&r.job_id===p.job_id&&same(r.binding,b)&&r.concept_id===selected.concept_id&&r.meaning_hash===selected.meaning_hash,'ADMIN_EXAMPLE_ACCEPTED_RESULT_CHANGED');
  check(recordId===await key('functional-example-review',{concept_id:r.concept_id,meaning_hash:r.meaning_hash,binding:omit(b,['prepared_at']),job_id:r.job_id,request_hash:p.request_hash,result_hash:p.result_hash,accepted_item_hash:p.accepted_item_hash}),'ADMIN_EXAMPLE_REVIEW_CHANGED');
  return r;
}

export async function validateExampleBinding(node,recordId,binding,options={}){
  const fail=options.fail??((code,status=409)=>{throw new ReleasePageError(code,status);});
  const r=await validateExampleReview(node,recordId,options),child=r.binding.children[binding.child_id],answer=r.result.decisions.find(v=>v.child_id===binding.child_id);
  if(r.record_hash!==binding.record_hash||r.concept_id!==binding.concept_id||r.meaning_hash!==binding.meaning_hash||!child||answer?.decision!=='same'||child.semantic_id!==binding.semantic_id||child.association_hash!==binding.association_hash||(binding.description_hash!==undefined&&child.description_hash!==binding.description_hash)||binding.observation_hashes?.[child.observation_id]!==child.observation_hash||(binding.reason!==undefined&&binding.reason!==answer.reason.split(WS).filter(Boolean).join(' ')))fail('ADMIN_EXAMPLE_MEMBERSHIP_PROOF_INVALID',409);
  if(binding.prepared_at!==undefined&&(typeof binding.prepared_at!=='string'||!Number.isFinite(Date.parse(binding.prepared_at))||Date.parse(binding.prepared_at)<Date.parse(r.reviewed_at)))fail('ADMIN_EXAMPLE_MEMBERSHIP_PROOF_INVALID',409);
  return r;
}
