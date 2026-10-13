import { spawnSync } from 'node:child_process';

let saved;
export function actualExampleFixtures(){
  if(saved)return structuredClone(saved);
  const result=spawnSync('.venv/bin/python',['-c',`
import json,tempfile,copy
from pathlib import Path
from unittest.mock import patch
from product_radar.frozen_records import freeze_original_json
from product_radar.functional_example_review import CONTRACT,_record_id,review_functional_examples
from product_radar.functional_types import prepare_example_memberships,accept_functional_definition,effective_functional_definition
from product_radar.models import digest
from product_radar.report_examples import freeze_functional_examples
from product_radar.report_narration import generate_report_narratives
from test_functional_example_review import FunctionalExampleReviewTests,Runner,NOW
from test_fit_pipeline import WritingFixture
from test_functional_report_packet import functional_packet_fixture
import test_functional_source_market as pool_fixture

fixture=FunctionalExampleReviewTests();fixture.setUp()
try:
 class FractionalRunner(Runner):
  def __call__(self,*args):
   answer,usage=super().__call__(*args);usage['synthetic_duration_seconds']=0.125;return answer,usage
 result=fixture.review(FractionalRunner());assert not result['failures'],result
 prepare_example_memberships(fixture.memory,fixture.cid,as_of=NOW)
 state=fixture.memory.snapshot();rid,row=next((i,r) for i,r in state['source_checks'].items() if r.get('kind')==CONTRACT)
 members=[r for r in state['source_checks'].values() if r.get('kind')=='concept-example-v1' and r.get('example_review')]
 variants={}
 def keep(name,bad):
  bad['record_hash']=digest({k:v for k,v in bad.items() if k!='record_hash'})
  variants[name]={'id':_record_id(bad),'wrapper':freeze_original_json(bad)}
 bad=copy.deepcopy(row);bad['result']['decisions'][0]['decision']='different';keep('stored_decision',bad)
 bad=copy.deepcopy(row);bad['job_proof']['accepted_item']['decisions'][0]['decision']='different';bad['job_proof']['accepted_item_hash']=digest(bad['job_proof']['accepted_item']);keep('accepted_decision',bad)
 bad=copy.deepcopy(row);bad['job_proof']['result_fingerprint'][0]=True;keep('fingerprint',bad)
 bad=copy.deepcopy(row);bad['job_proof']['original_job']['request']['model']='unapproved-model';keep('model',bad)
 bad=copy.deepcopy(row);bad['job_proof']['result_receipt']['response']['results'].append(copy.deepcopy(bad['job_proof']['result_receipt']['response']['results'][0]));bad['job_proof']['result_hash']=digest(bad['job_proof']['result_receipt']);keep('duplicate',bad)
 bad=copy.deepcopy(row);bad['reviewed_at']='2026-10-04T10:00:00+00:00';keep('review_before_completion',bad)
 bad=copy.deepcopy(row);bad['reviewed_at']='2026-10-11T10:00:00+00:00';keep('future_review',bad)
 normal={'id':rid,'wrapper':freeze_original_json(row),'members':members,'block':freeze_functional_examples(state,fixture.cid,as_of=NOW)}
finally: fixture.doCleanups()
partial=FunctionalExampleReviewTests();partial.setUp()
try:
 partial.test_partial_saved_original_recovers_only_current_children()
 state=partial.memory.snapshot();rid,row=next((i,r) for i,r in state['source_checks'].items() if r.get('kind')==CONTRACT)
 partial_row={'id':rid,'wrapper':freeze_original_json(row)}
finally: partial.doCleanups()

original=pool_fixture.accept_functional_definition
def accept_and_review(memory,cid,definition,**options):
 options['example_decisions']={}
 accepted=original(memory,cid,definition,**options)
 review=review_functional_examples(memory,[cid],Runner(),as_of=options['reviewed_at'])
 assert not review['failures'],review
 return accepted
with tempfile.TemporaryDirectory() as folder,patch.object(pool_fixture,'accept_functional_definition',accept_and_review),patch('product_radar.research_ai.utc_now',return_value=NOW):
 memory,pool,issue,data=functional_packet_fixture(Path(folder))
 assert len(pool['eligible'])==10
 assert all(p.get('example_review_proofs') for p in data['products'])
 assert all(any(e.get('example_review_id') for entries in p['functional_fit_proof']['examples'].values() for e in entries) for p in data['products'])
 output=generate_report_narratives(memory,issue,data,WritingFixture());assert not output['failures'],output
 facts=output['dataset']
 manifest={'schema_version':2,'fit_policy_id':'fit-10-v1','criterion_ids':facts['criterion_ids'],'kpi_total':10,
 'workbook_contract':'fit-checklist-examples-v2','recommendation_contract':facts['recommendation_contract'],
 'opportunity_policy_id':facts['opportunity_policy_id'],'languages':['en','hi-Latn'],'asset_receipt_hash':digest(facts['report_asset_receipt'])}
 first=facts['products'][0];cid=first['concept_id'];state=memory.snapshot();meaning=effective_functional_definition(state,cid)
 definition=state['source_checks'][meaning['definition_id']]
 child=next(iter(first['functional_fit_proof']['examples'].values()))[0]['child_id']
 accept_functional_definition(memory,cid,definition['definition'],basis_refs=sorted(definition['basis_refs']),
  reviewer='synthetic_operator',reviewed_at=NOW,expected_meaning_hash=meaning['meaning_hash'],
  example_decisions={child:{'decision':'different','reason':'Explicit saved correction.'}})
 current_block=freeze_functional_examples(memory.snapshot(),cid,as_of=NOW)
 print(json.dumps({'normal':normal,'partial':partial_row,'variants':variants,'current_definition':current_block,
  'packet':{'facts':facts,'manifest':manifest}},separators=(',',':')))
`],{cwd:process.cwd(),env:{...process.env,PYTHONPATH:'src:tests'},encoding:'utf8',maxBuffer:32*1024*1024});
  if(result.status!==0)throw new Error(result.stderr||result.stdout);
  saved=JSON.parse(result.stdout);return structuredClone(saved);
}
