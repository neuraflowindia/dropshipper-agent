import test from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { actualExampleFixtures } from './example-fixtures.test-helper.mjs';
import { readFunctionalOriginal } from './alias-proof.mjs';
import { validateExampleReview,validateExampleBinding } from './example-proof.mjs';
import { validateFunctionalPacket,canonical } from './release-admin.mjs';
globalThis.crypto??=webcrypto;
const NOW='2026-10-09T10:00:00+00:00';
const fail=(code,status=409)=>{throw new Error(code+':'+status);};

test('actual saved B example answer is deduped and proves each admitted original child',async()=>{
  const f=actualExampleFixtures().normal,node=await readFunctionalOriginal(f.wrapper);
  await validateExampleReview(node,f.id,{asOf:NOW});
  assert.match(f.wrapper.canonical_json,/"synthetic_duration_seconds":0\.125/);
  assert.equal(Object.keys(f.block.example_review_proofs).length,1);
  assert.equal(f.members.length,2);
  for(const m of f.members)await validateExampleBinding(node,f.id,{...m,record_hash:m.example_review.record_hash},{asOf:NOW});
  const bad=structuredClone(f.members[0]);bad.association_hash='a'.repeat(64);
  await assert.rejects(validateExampleBinding(node,f.id,{...bad,record_hash:bad.example_review.record_hash},{asOf:NOW}),/MEMBERSHIP_PROOF/);
});
test('original full reply can admit only its still-current saved subset without changing the reply',async()=>{
  const f=actualExampleFixtures().partial,node=await readFunctionalOriginal(f.wrapper);
  await validateExampleReview(node,f.id,{asOf:NOW});
  assert.equal(Object.keys(node.value.binding.children).length,1);
  assert.equal(node.value.job_proof.item.examples.length,2);
});
test('resealing records cannot change the original answer, job, duplicate reply, fingerprint or review date',async()=>{
  const variants=actualExampleFixtures().variants;
  for(const [name,f]of Object.entries(variants))await assert.rejects(validateExampleReview(await readFunctionalOriginal(f.wrapper),f.id,{asOf:NOW}),undefined,name);
});
test('ten actual reviewed example groups reach E, allocation, bilingual writing and portable packet checks',async()=>{
  const p=actualExampleFixtures().packet;
  await validateFunctionalPacket(p.facts,p.manifest,{canonical,fail,asOf:NOW});
  for(const product of p.facts.products){
    assert.ok(Object.keys(product.example_review_proofs).length);
    const refs=new Set(Object.values(product.functional_fit_proof.examples).flat().map(e=>e.example_review_id));
    for(const ref of refs)assert.ok(product.functional_fit_reference_proofs.records.source_checks[ref]);
  }
});
test('portable packet rejects removed, extra, wrong-child and unsupported new-method proof',async()=>{
  const p=actualExampleFixtures().packet;
  for(const mode of ['missing','extra','wrong_child','without_reference']){
    const facts=structuredClone(p.facts),product=facts.products[0],rid=Object.keys(product.example_review_proofs)[0];
    if(mode==='missing')delete product.example_review_proofs[rid];
    if(mode==='extra')product.example_review_proofs['unused-review']=product.example_review_proofs[rid];
    if(mode==='wrong_child'){const entry=Object.values(product.functional_fit_proof.examples)[0][0];entry.example_review_id='unknown-review';}
    if(mode==='without_reference')delete Object.values(product.example_bindings)[0].record.example_review;
    await assert.rejects(validateFunctionalPacket(facts,p.manifest,{canonical,fail,asOf:NOW}),undefined,mode);
  }
});
test('a current explicit type judgment cannot be overridden by borrowing the older saved E proof',async()=>{
  const f=actualExampleFixtures(),p=f.packet,facts=structuredClone(p.facts),product=facts.products[0],block=f.current_definition;
  delete product.example_review_proofs;delete product.example_selection;
  Object.assign(product,block);
  const proof=block.functional_definition_proof;
  const { releaseDigest }=await import('./release-pages.mjs');
  product.source_revisions.source_checks[proof.id]=await releaseDigest(proof.record);
  await assert.rejects(validateFunctionalPacket(facts,p.manifest,{canonical,fail,asOf:NOW}),/ADMIN_FUNCTIONAL_FIT_EXAMPLE_CHANGED/);
});
