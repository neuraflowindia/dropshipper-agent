import test from 'node:test';
import assert from 'node:assert/strict';
import {validateAliasProof} from './alias-proof.mjs';
import {actualAliasFixtures} from './alias-fixtures.test-helper.mjs';
const NOW='2026-10-09T10:00:00.000Z';
test('portable aliases verify actual exact/operator/original fake-model records',async()=>{
  for(const mode of ['exact','operator','model']){
    const proof=actualAliasFixtures()[mode],checked=await validateAliasProof(proof,{asOf:NOW});
    assert.equal(Object.keys(checked.records).length,1);
    assert.deepEqual(Object.values(checked.records)[0].row.input.member_ids,['bear','bird']);
    assert.equal(Object.values(checked.records)[0].row.method,mode==='exact'?'exact_meaning':mode);
  }
});
test('record routing/meaning/hash and predecessor changes cannot become projections',async()=>{
  const base=actualAliasFixtures().model;
  for(const mutate of [p=>{Object.values(p.records)[0].sha256='f'.repeat(64);},p=>{delete p.definitions[Object.keys(p.definitions)[0]];},p=>{p.records.fake=Object.values(p.records)[0];},p=>{p.projection={member_ids:['bear','bird']};}]){
    const proof=structuredClone(base);mutate(proof);await assert.rejects(validateAliasProof(proof,{asOf:NOW}));
  }
});
test('future original reviews cannot enter a current alias mutation',async()=>{
  await assert.rejects(validateAliasProof(actualAliasFixtures().model,{asOf:'2026-10-06T10:00:00+00:00'}));
});
test('actual negative B originals remain validated explicit judgments',async()=>{
  const checked=await validateAliasProof(actualAliasFixtures().negative,{asOf:NOW});
  assert.equal(Object.keys(checked.reviews).length,1);
  assert.equal(Object.values(checked.reviews)[0].row.result.decision,'different');
});
