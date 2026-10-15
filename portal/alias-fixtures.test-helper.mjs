// Actual local Python constructors with synthetic saved captures and a fake B
// runner. No provider, production data, external service or real model call.
import { spawnSync } from 'node:child_process';
import { PYTHON } from './python.test-helper.mjs';
let cached;
export function actualAliasFixtures() {
  if(cached)return structuredClone(cached);
  const script=String.raw`
import json
from unittest.mock import patch
import test_functional_alias_review as f
from product_radar.functional_aliases import accept_functional_alias, portable_alias_proof, KIND
from product_radar.functional_alias_review import _save_review
from product_radar.research_ai import Jobs
from product_radar.frozen_records import freeze_original_json
out={}
for mode in ('exact','operator','model','negative'):
 t=f.FunctionalAliasReviewTests();t.setUp()
 clock=patch('product_radar.research_ai.utc_now',return_value='2026-10-08T10:00:00+00:00');clock.start()
 try:
  if mode!='exact':t.different_wording()
  p=t.prepared()
  if mode=='model':
   reviewed=next(iter(t.run_review(f.Runner())['accepted'].values()))
   result=accept_functional_alias(t.memory,p['input'],method='model',reviewer='synthetic_owner',reason=reviewed['reason'],job_id=reviewed['job_proof']['job_id'],item_id=reviewed['item_id'])
  else:
   result=accept_functional_alias(t.memory,p['input'],method='exact_meaning' if mode=='exact' else 'operator',reviewer='synthetic_owner',reason='Saved actual narrow definitions reviewed.')
  if mode=='negative':
   class Negative(f.Runner):identity='synthetic-negative-review'
   r=Negative(decision='different');jobs=Jobs(t.memory,r)
   saved=jobs.run('B',p['payload']);jid=next(j for j,row in t.memory.snapshot()['jobs'].items() if row.get('request',{}).get('runner')==r.identity)
   _save_review(t.memory,p['input'],p['item'],jid)
  proof=portable_alias_proof(t.memory.snapshot(),[result['record_id']])
  out[mode]=proof
 finally:clock.stop();t.doCleanups()
print(json.dumps(out,ensure_ascii=False,separators=(',',':')))
`;
  const result=spawnSync(PYTHON,['-c',script],{cwd:new URL('..',import.meta.url),env:{...process.env,PYTHONPATH:'src:tests'},maxBuffer:8*1024*1024,timeout:30000});
  if(result.status!==0||!result.stdout?.length)throw new Error('Offline alias fixture failed: '+(result.stderr?.toString()??result.error?.message??''));
  cached=JSON.parse(result.stdout.toString());
  return structuredClone(cached);
}
