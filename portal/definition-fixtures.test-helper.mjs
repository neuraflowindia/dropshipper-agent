// Genuine saved A/B -> bounded B definition -> optional portable acceptance.
// The runner is fictional; no provider, cloud, publication or real model calls.
import { spawnSync } from 'node:child_process';
import { PYTHON } from './python.test-helper.mjs';
let cached;
export function actualDefinitionFixtures() {
  if(cached)return structuredClone(cached);
  const script=String.raw`
import json,copy
from test_functional_definition_review import FunctionalDefinitionReviewTests,Runner,NOW
from product_radar.functional_types import _DEFINITION_INTENT_FIELDS,prepare_example_memberships
from product_radar.report_examples import freeze_functional_examples,validate_functional_examples
from product_radar.functional_aliases import prepare_alias_input,accept_functional_alias,portable_alias_proof
from product_radar.frozen_records import freeze_original_json
from product_radar.models import digest
from product_radar.research_memory import key
out={}
for mode in ('normal','normalized','fractional'):
 t=FunctionalDefinitionReviewTests();t.setUp()
 try:
  def normalize(rows):
   for row in rows:
    row['definition']['report_title']='  '+row['definition']['report_title']+'  '
    row['definition']['included_distinctions']=row['definition']['included_distinctions']*2
    row['references'].reverse();row['example_decisions'].reverse()
    for child in row['example_decisions']:child['reason']='  An actual\n fictional example.  '
  class Fractional(Runner):
   def __call__(self,*args):
    response,usage=super().__call__(*args)
    usage['synthetic_duration_seconds']=0.125
    return response,usage
  runner=Fractional() if mode=='fractional' else Runner(mutate=normalize if mode=='normalized' else None)
  result=t.run_review(runner);assert not result['failures'],result
  current,row=t.definition();cid=current['concept_id'];rid=current['definition_id']
  prepare_example_memberships(t.memory,cid,as_of=NOW)
  block=freeze_functional_examples(t.memory.snapshot(),cid,as_of=NOW)
  validate_functional_examples(block,concept_id=cid)
  prepared=prepare_alias_input(t.memory.snapshot(),t.cids,reviewed_at=NOW)
  alias=accept_functional_alias(t.memory,prepared,method='operator',reviewer='synthetic_owner',reason='Actual synthetic definitions explicitly reviewed.')
  proof=portable_alias_proof(t.memory.snapshot(),[alias['record_id']])
  out[mode]={'id':rid,'wrapper':freeze_original_json(row),'block':block,'aliases':proof}
  if mode=='fractional':
   variants={}
   def reseal(value):
    value['record_hash']=digest({k:v for k,v in value.items() if k!='record_hash'})
    return {'id':key('functional-definition',{k:value[k] for k in _DEFINITION_INTENT_FIELDS}),'wrapper':freeze_original_json(value)}
   for name in ('definition','basis','child_decision','accepted','duplicate','fingerprint','job_model','outer_job','future','review_precedes_completion','semantic_binding'):
    v=copy.deepcopy(row);p=v['job_proof']['definition_acceptance']
    if name=='definition':
     v['definition']['primary_use']='An invented different use.'
     v['meaning_hash']=digest({k:v['definition'][k] for k in ('primary_use','mechanism','use_context','compatibility_constraints','included_distinctions','excluded_distinctions')})
    elif name=='basis':v['basis_refs'][next(iter(v['basis_refs']))]='e'*64
    elif name=='child_decision':v['example_decisions'][next(iter(v['example_decisions']))]['decision']='different'
    elif name=='accepted':
     p['accepted_item']['reason']='Not the actual original reply.';p['accepted_item_hash']=digest(p['accepted_item'])
    elif name=='duplicate':
     p['result_receipt']['response']['results'].append(copy.deepcopy(p['accepted_item']))
     p['result_hash']=digest(p['result_receipt']);v['job_proof']['result_hash']=p['result_hash']
    elif name=='fingerprint':p['result_fingerprint'][0]=True
    elif name=='job_model':p['original_job']['request']['model']='invented-model'
    elif name=='outer_job':v['job_id']='job-'+'a'*40
    elif name in ('future','review_precedes_completion'):
     p['result_receipt']['completed_at']='2026-10-10T10:00:00+00:00'
     p['result_hash']=digest(p['result_receipt']);v['job_proof']['result_hash']=p['result_hash']
     if name=='future':v['reviewed_at']=p['result_receipt']['completed_at']
    elif name=='semantic_binding':p['binding']['children'][next(iter(p['binding']['children']))]['semantic_id']='different-semantic'
    variants[name]=reseal(v)
   out['variants']=variants
 finally:t.doCleanups()
print(json.dumps(out,ensure_ascii=False,separators=(',',':')))
`;
  const r=spawnSync(PYTHON,['-c',script],{cwd:new URL('..',import.meta.url),env:{...process.env,PYTHONPATH:'src:tests'},maxBuffer:8*1024*1024,timeout:30000});
  if(r.status!==0||!r.stdout?.length)throw new Error('Offline definition fixture failed: '+(r.stderr?.toString()??r.error?.message??''));
  cached=JSON.parse(r.stdout.toString());return structuredClone(cached);
}
