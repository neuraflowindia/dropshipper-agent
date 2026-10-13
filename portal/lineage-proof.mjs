// Saved operator-reviewed narrowing originals. This is directional history,
// not an alias, source endorsement, model inference or publication authority.
import { readFunctionalOriginal, readFunctionalDefinitions, validateFunctionalDefinitionInput } from './alias-proof.mjs';
import { ReleasePageError, releaseCanonical, releaseDigest, releaseHash } from './release-pages.mjs';
const enc=new TextEncoder(),ID=/^[A-Za-z0-9_-]{1,80}$/,MEANING=['primary_use','mechanism','use_context','compatibility_constraints','included_distinctions','excluded_distinctions'];
const fail=(code='ADMIN_LINEAGE_PROOF_INVALID')=>{throw new ReleasePageError(code,409);};
const check=(v,code)=>{if(!v)fail(code);};
const object=v=>v!==null&&typeof v==='object'&&!Array.isArray(v);
const exact=(v,fields)=>object(v)&&Object.keys(v).sort().join('|')===[...fields].sort().join('|');
const clean=v=>typeof v==='string'&&v.length>0&&v.length<=2000&&v===v.split(/[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/u).filter(Boolean).join(' ');
const same=(a,b)=>releaseCanonical(a)===releaseCanonical(b);
const ordered=rows=>[...rows].sort((a,b)=>{const x=Array.from(a,c=>c.codePointAt(0)),y=Array.from(b,c=>c.codePointAt(0));for(let i=0;i<Math.min(x.length,y.length);i++)if(x[i]!==y[i])return x[i]-y[i];return x.length-y.length;});
const rawHash=(node,omit=[])=>releaseHash(enc.encode('{'+Object.keys(node.children).filter(k=>!omit.includes(k)).sort().map(k=>releaseCanonical(k)+':'+node.children[k].raw).join(',')+'}'));
const key=async(prefix,value)=>prefix+'-'+(await releaseDigest(value)).slice(0,40);
function date(v){check(typeof v==='string'&&/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(v));const at=Date.parse(v);check(Number.isFinite(at)&&new Date(at).toISOString().slice(0,19)===v.slice(0,19));return at;}

export async function validateLineageProof(proof,{asOf}={}){
  check(exact(proof,['contract','records','definitions','observations','descriptions'])&&proof.contract==='functional-lineage-portable-v1'&&[proof.records,proof.definitions,proof.observations,proof.descriptions].every(object));
  check(Object.keys(proof.records).length>=1&&Object.keys(proof.records).length<=20&&enc.encode(releaseCanonical(proof)).length<=480*1024,'ADMIN_LINEAGE_PROOF_BATCH_REQUIRED');
  const at=date(asOf),{definitions,groups}=await readFunctionalDefinitions(proof.definitions,{asOf}),observations={},descriptions={},records={},usedDefinitions=new Set(),requiredDefinitionIds=new Set(),usedObservations=new Set(),usedDescriptions=new Set();
  for(const [id,w]of Object.entries(proof.observations)){check(ID.test(id));observations[id]=await readFunctionalOriginal(w);}
  for(const [id,w]of Object.entries(proof.descriptions)){check(ID.test(id));descriptions[id]=await readFunctionalOriginal(w);}
  for(const [id,w]of Object.entries(proof.records)){
    const n=await readFunctionalOriginal(w),r=n.value;
    check(exact(r,['kind','input','relationship','semantic_basis','distinctions','reviewer','reason','record_hash'])&&r.kind==='functional-lineage-v1'&&r.relationship==='narrower'&&['exact_use_mechanism','operator_reviewed'].includes(r.semantic_basis)&&clean(r.reviewer)&&clean(r.reason));
    check(r.record_hash===await rawHash(n,['record_hash'])&&id==='functional-lineage-'+(await rawHash(n,['record_hash'])).slice(0,40),'ADMIN_LINEAGE_ORIGINAL_CHANGED');
    const input=r.input,ni=n.children.input;
    check(exact(input,['contract','parent_id','child_id','definition_input','grounding_bindings','input_hash'])&&input.contract==='functional-lineage-input-v1'&&ID.test(input.parent_id??'')&&ID.test(input.child_id??'')&&input.parent_id!==input.child_id&&input.input_hash===await rawHash(ni,['input_hash'])&&enc.encode(ni.raw).length<=256*1024);
    await validateFunctionalDefinitionInput(input.definition_input,definitions,at);
    const members=[input.parent_id,input.child_id].sort();check(same(input.definition_input.member_ids,members)&&exact(input.grounding_bindings,members));members.forEach(cid=>{usedDefinitions.add(cid);const bound=definitions[input.definition_input.definition_bindings[cid].definition_id].value.revision;for(const [rid,r]of groups.get(cid))if(r.revision<=bound)requiredDefinitionIds.add(rid);});
    const parent=definitions[input.definition_input.definition_bindings[input.parent_id].definition_id].value.definition,child=definitions[input.definition_input.definition_bindings[input.child_id].definition_id].value.definition;
    check(!MEANING.every(f=>same(parent[f],child[f])),'ADMIN_LINEAGE_MEANINGFUL_NARROWING_REQUIRED');
    check(r.semantic_basis!=='exact_use_mechanism'||['primary_use','mechanism'].every(f=>parent[f]===child[f]),'ADMIN_LINEAGE_OPERATOR_REVIEW_REQUIRED');
    check(Array.isArray(r.distinctions)&&r.distinctions.length>=1&&r.distinctions.length<=20&&r.distinctions.every(clean)&&same(r.distinctions,ordered(new Set(r.distinctions))));
    for(const cid of members){
      const refs=input.grounding_bindings[cid];check(object(refs)&&Object.keys(refs).length>=1&&Object.keys(refs).length<=20);
      for(const [oid,p]of Object.entries(refs)){
        const observation=observations[oid],a=ni.children.grounding_bindings.children[cid].children[oid].children.association;
        check(ID.test(oid)&&exact(p,['observation_hash','association_id','association_hash','association'])&&observation&&p.observation_hash===observation.hash&&ID.test(p.association_id??'')&&object(p.association)&&p.association_hash===await releaseHash(enc.encode(a.raw)),'ADMIN_LINEAGE_GROUNDING_CHANGED');
        const o=observation.value,description=descriptions[o.semantic_id];
        check(description&&object(description.value.result)&&Array.isArray(description.value.result.products)&&p.association.semantic_id===o.semantic_id&&p.association.concept_id===cid&&p.association.status==='resolved'&&date(o.observed_at)<=date(input.definition_input.reviewed_at),'ADMIN_LINEAGE_GROUNDING_CHANGED');
        const rh=await releaseHash(enc.encode(description.children.result.raw));let found=false;
        for(let index=0;index<description.value.result.products.length;index++)if(p.association_id===await key('child',[o.semantic_id,index,rh])){found=true;break;}
        check(found,'ADMIN_LINEAGE_GROUNDING_CHANGED');usedObservations.add(oid);usedDescriptions.add(o.semantic_id);
      }
    }
    records[id]={row:r,hash:n.hash,wrapper:w};
  }
  check([...groups.keys()].every(cid=>usedDefinitions.has(cid))&&Object.keys(definitions).length===requiredDefinitionIds.size&&Object.keys(observations).every(id=>usedObservations.has(id))&&Object.keys(descriptions).every(id=>usedDescriptions.has(id)),'ADMIN_LINEAGE_UNUSED_ORIGINAL');
  return {records,definitions,observations,descriptions,proof_hash:await releaseDigest(proof)};
}
