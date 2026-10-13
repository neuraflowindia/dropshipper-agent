import test from 'node:test';
import assert from 'node:assert/strict';
import {validateLineageProof} from './lineage-proof.mjs';
import {actualLineageFixtures} from './lineage-fixtures.test-helper.mjs';
const AT='2026-10-09T10:00:00.000Z';
test('genuine operator-reviewed narrowings carry exact historical definitions and actual grounding',async()=>{
  for(const mode of ['one','chain','siblings','reverse']){const proof=actualLineageFixtures()[mode],checked=await validateLineageProof(proof,{asOf:AT});assert.equal(Object.keys(checked.records).length,Object.keys(proof.records).length);}
});
test('changed originals, extra/missing dependencies and future reviews cannot be relabelled as narrowing history',async()=>{
  const proof=actualLineageFixtures().one;
  for(const edit of [p=>p.records[Object.keys(p.records)[0]].sha256='0'.repeat(64),p=>delete p.observations[Object.keys(p.observations)[0]],p=>p.observations.extra=p.observations[Object.keys(p.observations)[0]],p=>delete p.definitions[Object.keys(p.definitions)[0]],p=>p.descriptions.extra=p.descriptions[Object.keys(p.descriptions)[0]]]){const changed=structuredClone(proof);edit(changed);await assert.rejects(validateLineageProof(changed,{asOf:AT}));}
  await assert.rejects(validateLineageProof(proof,{asOf:'2026-10-05T10:00:00.000Z'}),/FUTURE|INVALID/);
});
