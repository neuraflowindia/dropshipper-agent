// Actual saved Python constructors and portable producer, synthetic data only.
import {spawnSync} from 'node:child_process';
let cached;
export function actualLineageFixtures(){
  if(cached)return structuredClone(cached);
  const script=String.raw`
import json
import test_functional_lineage as f
from product_radar.functional_lineage_portable import portable_lineage_proof
from product_radar.functional_lineage import accept_functional_lineage
from product_radar.functional_aliases import prepare_alias_input,accept_functional_alias,portable_alias_proof
out={}
for mode,pairs in {'one':[('bear','bird')],'chain':[('bird','cat'),('bear','bird')],'siblings':[('bear','bird'),('bear','cat')],'reverse':[('bird','bear')]}.items():
 t=f.FunctionalLineageTests();t.setUp()
 try:
  ids=[t.accept(parent,child)['record_id'] for parent,child in pairs]
  out[mode]=portable_lineage_proof(t.memory.snapshot(),ids)
  if mode=='one':
   extra=accept_functional_lineage(t.memory,t.prepared(),reviewer='synthetic_operator',reason='A second actual explicit review of the same narrowing.',distinctions=['The same narrower shelf boundary.'])
   out['extra']=portable_lineage_proof(t.memory.snapshot(),[extra['record_id']])
  if mode=='siblings':
   prepared=prepare_alias_input(t.memory.snapshot(),['bird','cat'],reviewed_at='2026-10-06T12:00:00+00:00')
   result=accept_functional_alias(t.memory,prepared,method='operator',reviewer='synthetic_owner',reason='The two narrower definitions were explicitly reviewed together.')
   out['alias_siblings']=portable_alias_proof(t.memory.snapshot(),[result['record_id']])
 finally:t.doCleanups()
print(json.dumps(out,ensure_ascii=False,separators=(',',':')))
`;
  const result=spawnSync('.venv/bin/python',['-c',script],{cwd:new URL('..',import.meta.url),env:{...process.env,PYTHONPATH:'src:tests'},maxBuffer:8*1024*1024,timeout:30000});
  if(result.status!==0||!result.stdout?.length)throw new Error('Offline lineage fixture failed: '+(result.stderr?.toString()??result.error?.message??''));
  cached=JSON.parse(result.stdout.toString());return structuredClone(cached);
}
