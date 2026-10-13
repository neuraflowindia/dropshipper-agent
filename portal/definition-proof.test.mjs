import test from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { actualDefinitionFixtures } from './definition-fixtures.test-helper.mjs';
import { readFunctionalDefinitions,readFunctionalOriginal,validateAliasProof } from './alias-proof.mjs';
import { validateDefinitionAcceptance } from './definition-proof.mjs';
globalThis.crypto??=webcrypto;
const NOW='2026-10-09T10:00:00.000Z';

test('actual B definition acceptance keeps large file timestamps and fractional original usage exact',async()=>{
  const fixtures=actualDefinitionFixtures();
  for(const mode of ['normal','normalized','fractional']){
    const f=fixtures[mode],node=await readFunctionalOriginal(f.wrapper);
    const answer=await validateDefinitionAcceptance(node,{asOf:NOW});
    assert.equal(answer.decision,'prepared');
    assert.deepEqual(answer.definition,JSON.parse(JSON.stringify(node.value.definition)));
    assert.equal(f.block.functional_definition_proof.record.contract,'frozen-original-json-v1');
    assert.equal(f.block.functional_definition_proof.record.sha256,f.wrapper.sha256);
    const checked=await readFunctionalDefinitions({[f.id]:f.wrapper},{asOf:NOW});
    assert.equal(checked.definitions[f.id].hash,f.wrapper.sha256);
    assert.ok(Object.values(node.children.job_proof.children.definition_acceptance.children.result_fingerprint.children).some(n=>/^\d{17,}$/.test(n.raw)));
  }
  assert.match(fixtures.fractional.wrapper.canonical_json,/"synthetic_duration_seconds":0\.125/);
});
test('shared alias/lineage definition reader authenticates the actual saved B capsule',async()=>{
  for(const mode of ['normal','normalized','fractional']){
    const proof=actualDefinitionFixtures()[mode].aliases,checked=await validateAliasProof(proof,{asOf:NOW});
    assert.equal(Object.keys(checked.records).length,1);
    assert.equal(Object.keys(checked.definitions).length,2);
  }
});
test('resealed outer records cannot invent definition, basis, child decisions, original job or accepted answer',async()=>{
  const variants=actualDefinitionFixtures().variants;
  for(const name of ['definition','basis','child_decision','accepted','duplicate','fingerprint','job_model','outer_job','semantic_binding','review_precedes_completion']){
    const f=variants[name];await assert.rejects(readFunctionalDefinitions({[f.id]:f.wrapper},{asOf:NOW}),undefined,name);
  }
});
test('delayed original completion needs a genuine later acceptance date and actual server clock',async()=>{
  const f=actualDefinitionFixtures().variants.future;
  await assert.rejects(readFunctionalDefinitions({[f.id]:f.wrapper},{asOf:NOW}),/ADMIN_DEFINITION_RESULT_FUTURE/);
  const checked=await readFunctionalDefinitions({[f.id]:f.wrapper},{asOf:'2026-10-11T10:00:00+00:00'});
  assert.equal(checked.definitions[f.id].value.reviewed_at,'2026-10-10T10:00:00+00:00');
});
