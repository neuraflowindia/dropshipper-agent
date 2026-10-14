import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { webcrypto, createHash } from 'node:crypto';
import { test } from 'node:test';
import { validateFitPacket, validateFunctionalPacket, canonical, handleAdminRequest } from './release-admin.mjs';
import { currentSupplierOutcome, retiredSupplierRoutes } from './functional-packet.mjs';

globalThis.crypto ??= webcrypto;
// The actual offline Python producers generate this fixture. No handwritten
// compatibility packet and no real source, model, upload or publication calls.
const generated = spawnSync('.venv/bin/python', ['-c', `
import json,tempfile,copy
from pathlib import Path
from test_functional_report_packet import functional_packet_fixture
from test_fit_pipeline import WritingFixture
from product_radar.report_narration import generate_report_narratives
from product_radar.models import digest
from product_radar.product_images import capture_product_image,inspect_image
from product_radar.report_selection import build_report_pool
from product_radar.report_examples import RECOMMENDATION_CONTRACT
from product_radar.report_source_facts import freeze_functional_route_proof
from product_radar.research_memory import import_capture
from product_radar.research_sources import map_research_rows
from product_radar.source_interpretation import review_source_results
from product_radar.suppliers import extract_supplier_terms
from product_radar.source_coverage import prepare_source_tasks,execute_source_run
from product_radar.fit_review import prepare_practical_fit_reviews,run_fit_reviews
from product_radar.fit_description import FUNCTIONAL_CONTRACT
from product_radar.functional_types import effective_functional_definition
from product_radar.functional_definition_review import review_functional_definitions
from test_functional_definition_review import Runner as DefinitionRunner
from product_radar.research_scope import accept_scoped_evidence
from product_radar.research_operations import import_trends_evidence
from test_functional_fit import FunctionalFitTests,FunctionalFitRunner
from test_functional_source_market import functional_pool_fixture,NOW
from test_report_scoped_suppliers import scoped_supplier_fixture,range_supplier_fixture
from test_report_assets import image_bytes
from test_functional_source_interpretation import Runner as SourceRunner
from test_suppliers import FixtureRunner as SupplierRunner,item as supplier_item,term as supplier_term,price as supplier_price
from test_research_sources import amazon_row,request as amazon_request
from test_trends_browser import metadata,weekly
from types import SimpleNamespace
from unittest.mock import patch
with tempfile.TemporaryDirectory() as path:
 memory,pool,issue,data=functional_packet_fixture(Path(path))
 result=generate_report_narratives(memory,issue,data,WritingFixture())
 assert not result['failures'],result
 facts=result['dataset']
 manifest={'schema_version':2,'fit_policy_id':'fit-10-v1','criterion_ids':facts['criterion_ids'],'kpi_total':10,
 'workbook_contract':'fit-checklist-examples-v2','recommendation_contract':facts['recommendation_contract'],
 'opportunity_policy_id':facts['opportunity_policy_id'],'languages':['en','hi-Latn'],
 'asset_receipt_hash':digest(facts['report_asset_receipt'])}
 cid=facts['products'][0]['concept_id']
 FunctionalFitTests.physical_fact(SimpleNamespace(memory=memory,cid=cid),field='weight',value=0.35)
 reviews=prepare_practical_fit_reviews(memory,concept_ids=[cid],as_of='2026-10-05T10:00:00+00:00',contract_version=FUNCTIONAL_CONTRACT)
 run_fit_reviews(memory,FunctionalFitRunner(),[r['review_id'] for r in reviews])
 chart=import_trends_evidence(memory,concept_id=cid,acquisition_id='portal-chart',csv_bytes=weekly(),metadata=metadata(collected_at='2026-10-05T10:00:00+00:00'))
 chart_original=memory.snapshot()['trends_evidence'][chart['id']]
 accept_scoped_evidence(memory,cid,route='google-trends',scope='narrow_type',relationship='relevant_family',support='insufficient',
 references=[{'table':'trends_evidence','id':chart['id'],'hash':digest(chart_original)}],
 usable_fields=[{'reference':{'table':'trends_evidence','id':chart['id']},'path':['parsed','series']}],
 limitations=['Relative search interest, not sales.'],reason='Explicit synthetic chart comparison.',reviewer='synthetic',
 as_of='2026-10-05T10:00:00+00:00',expected_meaning_hash=effective_functional_definition(memory.snapshot(),cid)['meaning_hash'])
 _,_,rich_issue,rich_data=functional_packet_fixture(Path(path)/'rich',memory=memory)
 rich=generate_report_narratives(memory,rich_issue,rich_data,WritingFixture())
 assert not rich['failures'],rich
 rich_manifest=dict(manifest,asset_receipt_hash=digest(rich_data['report_asset_receipt']))
 photo_memory,_=functional_pool_fixture(Path(path)/'photo-inputs',count=10,with_images=True)
 initial=build_report_pool(photo_memory,as_of=NOW,recommendation_contract=RECOMMENDATION_CONTRACT)
 first=initial['product_facts'][0]
 raw=image_bytes((400,200))
 def fetch(url):
  return {'state':'usable','reason':'Synthetic original photo saved.','requested_url':url,'checked_at':NOW,
   'dispatch_started':True,'body_complete':True,'metadata':inspect_image(raw)},raw
 photo_root=Path(path)/'photos'
 capture_product_image(photo_memory,first['concept_id'],first['image_candidates'][0],media_root=photo_root,fetcher=fetch,shared_acquisitions=True)
 with patch('product_radar.product_images.MEDIA_ROOT',photo_root),patch('product_radar.report_assets.MEDIA_ROOT',photo_root):
  _,_,photo_issue,photo_data=functional_packet_fixture(Path(path)/'photo',memory=photo_memory)
  photo=generate_report_narratives(photo_memory,photo_issue,photo_data,WritingFixture())
 assert not photo['failures'],photo
 photo_manifest=dict(manifest,asset_receipt_hash=digest(photo_data['report_asset_receipt']))
 def fetch_v3(url):
  receipt,body=fetch(url)
  receipt.update(final_url=url,http_status=200,response_received=True,owned_worker_exited=True,execution_stage='response_received',redirects=[])
  return receipt,body
 second=initial['product_facts'][1]
 capture_product_image(photo_memory,second['concept_id'],second['image_candidates'][0],media_root=photo_root,fetcher=fetch_v3,
  shared_acquisitions=True,renewable_acquisitions=True,as_of=NOW)
 for _ in range(3):
  result=review_source_results(photo_memory,SourceRunner(),as_of=NOW,limit=20)
  assert not [r for r in result['outcomes'] if r['state']=='waiting'],result
  if not result['remaining_questions']: break
 assert not result['remaining_questions'],result
 cid=photo_data['products'][0]['concept_id']
 prepare_source_tasks(photo_memory,[{'route':'meta-ads','request':{'query':'draining caddy holder','country':'IN'},'concept_ids':[cid]}],run_id='last-blocked-check',as_of=NOW)
 def blocked(context):
  context['record_dispatch']()
  return {'schema_version':1,'route':context['route'],'acquisition_id':context['acquisition_id'],'observed_at':NOW,
   'state':'blocked','dispatch_started':True,'reference':'original/receipt.json','capture_ids':[],'observation_ids':[],
   'target_results':[],'reason':'Synthetic actual access refusal.','retryable':True,'failure_scope':'route'}
 execute_source_run(photo_memory,'last-blocked-check',handlers={'meta-ads':blocked},as_of=NOW,bounded_call=lambda callback,context,timeout:callback(context))
 class ContextSupplierRunner(SupplierRunner):
  def __call__(self,role,payload,folder):
   response,usage=super().__call__(role,payload,folder)
   for item,row in zip(payload['items'],response['results']):
    page=item['pages'][0]['id']
    if item['pages'][0]['url'].endswith('/rejected'):
     row['relevance']['relationship']='unrelated'
    else:
     row['terms']=[supplier_term('single_piece_purchase','Yes','MOQ 1 piece.',reference=page),
      supplier_term('direct_customer_dispatch','Yes','We dispatch directly to your customer.',reference=page),
      supplier_term('subscription_requirement','Not required','No subscription required.',reference=page)]
     row['prices']=[supplier_price(references=[page],quotes=[{'page_id':page,'excerpt':'₹300 per pack of three; minimum 100 packs.'}])]
   return response,usage
 items=[supplier_item('supplier-'+suffix) for suffix in ['kept','rejected']]
 for item,suffix in zip(items,['kept','rejected']):
  item['product']=dict(photo_memory.snapshot()['concepts'][cid]['description'],concept_id=cid)
  item['pages'][0]['url']='https://supplier.example.org/'+suffix
 with patch('product_radar.suppliers.utc_now',lambda:NOW):
  extracted=extract_supplier_terms(photo_memory,items,ContextSupplierRunner(),source_dates={i['id']:{p['id']:NOW for p in i['pages']} for i in items})
 assert not extracted['failures'],extracted
 with patch('product_radar.product_images.MEDIA_ROOT',photo_root),patch('product_radar.report_assets.MEDIA_ROOT',photo_root):
  _,_,reviewed_issue,reviewed_data=functional_packet_fixture(Path(path)/'reviewed',memory=photo_memory)
  reviewed=generate_report_narratives(photo_memory,reviewed_issue,reviewed_data,WritingFixture())
 assert not reviewed['failures'],reviewed
 reviewed_manifest=dict(manifest,asset_receipt_hash=digest(reviewed_data['report_asset_receipt']))
 early_memory,_=functional_pool_fixture(Path(path)/'replacement-inputs',count=10,at='2026-10-05T09:00:00+00:00')
 _,_,early_issue,early_data=functional_packet_fixture(Path(path)/'replacement',memory=early_memory)
 early=generate_report_narratives(early_memory,early_issue,early_data,WritingFixture())
 assert not early['failures'],early
 replaced=copy.deepcopy(early['dataset'])
 p=replaced['products'][0]
 original=next(json.loads(row['canonical_json']) for row in p['functional_market_proof']['records']['observations'].values() if json.loads(row['canonical_json'])['source']=='amazon')
 cards=[dict(amazon_row(),asin=original['facts']['asin'],link=original['facts']['url'],title='Different replacement listing')]
 import_capture(early_memory,source='amazon',acquisition_id='actual-later-replacement',observed_at=NOW,
  mapped=map_research_rows(cards,amazon_request()),capture_hash=digest(cards),reference={'fixture':'synthetic'})
 p['functional_route_proof']=freeze_functional_route_proof(early_memory.snapshot(),p['concept_id'],as_of=NOW)
 for table,rows in p['functional_route_proof']['records'].items():
  p['source_revisions'].setdefault(table,{}).update({rid:row['sha256'] for rid,row in rows.items()})
 replaced_manifest=dict(manifest,asset_receipt_hash=digest(early_data['report_asset_receipt']))
 scoped_supplier_fixture(memory,cid=sorted(memory.snapshot()['concepts'])[0],candidate=True)
 _,_,scoped_issue,scoped_data=functional_packet_fixture(Path(path)/'scoped',memory=memory)
 scoped=generate_report_narratives(memory,scoped_issue,scoped_data,WritingFixture())
 assert not scoped['failures'],scoped
 scoped_manifest=dict(manifest,asset_receipt_hash=digest(scoped_data['report_asset_receipt']))
 range_supplier_fixture(memory,cid=sorted(memory.snapshot()['concepts'])[1])
 scoped_supplier_fixture(memory,cid=sorted(memory.snapshot()['concepts'])[2],at='2026-09-01T10:00:00+00:00',checked_at=NOW)
 _,_,range_issue,range_data=functional_packet_fixture(Path(path)/'range',memory=memory)
 range_report=generate_report_narratives(memory,range_issue,range_data,WritingFixture())
 assert not range_report['failures'],range_report
 range_manifest=dict(manifest,asset_receipt_hash=digest(range_data['report_asset_receipt']))
 def accept_model_definition(memory,cid,definition,**kwargs):
  class OriginalDefinitionRunner(DefinitionRunner):
   def __call__(self,*args):
    response,usage=super().__call__(*args)
    for row in response['results']:row['definition']=copy.deepcopy(definition)
    usage['synthetic_duration_seconds']=0.125
    return response,usage
  result=review_functional_definitions(memory,[cid],OriginalDefinitionRunner(),as_of=kwargs['reviewed_at'])
  assert not result['failures'],result
  return result['accepted'][cid]
 with patch('test_functional_source_market.accept_functional_definition',side_effect=accept_model_definition),patch('product_radar.research_ai.utc_now',return_value=NOW):
  model_memory,_=functional_pool_fixture(Path(path)/'model-definitions',count=10)
  scoped_supplier_fixture(model_memory,cid=sorted(model_memory.snapshot()['concepts'])[0],candidate=True)
  _,_,model_issue,model_data=functional_packet_fixture(Path(path)/'model-report',memory=model_memory)
  model_report=generate_report_narratives(model_memory,model_issue,model_data,WritingFixture())
  assert not model_report['failures'],model_report
 model_manifest=dict(manifest,asset_receipt_hash=digest(model_data['report_asset_receipt']))
 print(json.dumps({'facts':facts,'manifest':manifest,'rich':{'facts':rich['dataset'],'manifest':rich_manifest},
 'photo':{'facts':photo['dataset'],'manifest':photo_manifest},'reviewed':{'facts':reviewed['dataset'],'manifest':reviewed_manifest},
 'replaced':{'facts':replaced,'manifest':replaced_manifest},'scoped':{'facts':scoped['dataset'],'manifest':scoped_manifest},
 'range':{'facts':range_report['dataset'],'manifest':range_manifest},
 'model_definitions':{'facts':model_report['dataset'],'manifest':model_manifest}},ensure_ascii=False))
`], { encoding: 'utf8', maxBuffer: 12 * 1024 * 1024, env: { ...process.env, PYTHONPATH: 'src:tests' } });
assert.equal(generated.status, 0, generated.stderr);
const fixture = JSON.parse(generated.stdout);
const fresh = () => structuredClone(fixture);
const encoder=new TextEncoder(),sha=value=>createHash('sha256').update(value).digest('hex');
const encoded=value=>encoder.encode(canonical(value)+'\n');
function storedPacket(date='2026-10-08') {
  const {facts,manifest}=fresh();
  // These original-research fixtures are synthetic. The captured marker below
  // exercises only the wire contract, never claims real research/publication.
  Object.assign(facts,{evidence_type:'captured',release_date:date});
  const prefix=`clients/default/issues/${facts.issue_id}/r${facts.revision}/`,objects=new Map();
  const raw=encoded(facts), artifacts={facts:{path:'/offline/final_report_data.json',sha256:sha(raw),size:raw.length}};
  objects.set(prefix+'final_report_data.json',raw);
  const files=['shortlist.xlsx',...manifest.languages.map(l=>`report-${l}.pdf`)].map(name=>{
    const bytes=encoder.encode(name.endsWith('.pdf')?'%PDF-1.7\nOffline wire fixture only.':'PK offline wire fixture only.');
    objects.set(prefix+name,bytes);const record={path:'/offline/'+name,sha256:sha(bytes),size:bytes.length};
    artifacts[name==='shortlist.xlsx'?'workbook':'pdf-'+name.slice(7,-4)]=record;
    return {name,key:prefix+name,sha256:record.sha256,size:record.size,media_type:name.endsWith('.pdf')?'application/pdf':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'};
  });
  Object.assign(manifest,{client_id:'default',issue_id:facts.issue_id,revision:facts.revision,release_date:date,evidence_type:'captured',selection_hash:facts.selection_hash,
    facts_hash:sha(encoder.encode(canonical(facts))),complete:true,approved:false,release_ready:false,state:'draft_ready_for_review',publication_ordinal:1,
    concept_ids:facts.products.map(p=>p.concept_id),counts:{products:10,new:10,updates:0},product_count:10,new_count:10,files,artifacts});
  manifest.package_hash=sha(encoder.encode(canonical(manifest)));objects.set(prefix+'delivery-manifest.json',encoded(manifest));
  return {facts,manifest,objects};
}
function fakeBucket(packet) {
  const objects=new Map([...packet.objects].map(([key,bytes])=>[key,{bytes,etag:'fixture-object'}]));const calls=[];let serial=0;
  return {objects,calls,async get(key){calls.push(['get',key]);const record=objects.get(key);return record?{size:record.bytes.length,etag:record.etag,arrayBuffer:async()=>record.bytes.slice().buffer}:null;},
    async put(key,input){calls.push(['put',key]);const etag='offline-'+(++serial);objects.set(key,{bytes:new Uint8Array(input).slice(),etag});return {etag};}};
}
function activationRequest(packet,schedule) {
  const manifest=packet.manifest;
  return new Request('https://reports.example.com/admin/index',{method:'POST',headers:{'Content-Type':'application/json','X-NeuraFlow-Release-Action':'v1'},body:encoded({expected_etag:null,
    mutation:{kind:'activate',client_id:'default',manifest,scheduled_at:schedule,expected_previous:null,selected_language:null,allow_synthetic:false,
      approval:{approved:true,owner_id:'owner-one',client_id:'default',issue_id:manifest.issue_id,revision:manifest.revision,package_hash:manifest.package_hash,
        selection_hash:manifest.selection_hash,publication_ordinal:1,scheduled_at:schedule}}})});
}
const context={access:{aud:'worker-aud',getIdentity:async()=>({email:'operator@example.com'})}};
const environment=bucket=>({CLIENT_ID:'default',ACCESS_AUD:'worker-aud',ALLOWED_EMAILS:'["client@example.com"]',OPERATOR_EMAILS:'["operator@example.com"]',PUBLICATION_TIME:'09:00',ONGOING_LANGUAGE:'',REPORTS:bucket});

test('actual producers: schema-3 packet validates independently in the Worker reader', async () => {
  const {facts,manifest}=fresh();
  assert.equal(validateFitPacket(facts,manifest),facts);
  assert.equal(await validateFunctionalPacket(facts,manifest,{asOf:'2026-10-07T03:30:00Z'}),facts);
});

test('actual bounded B definitions flow through C/E, examples, source routes and both writing editions',async()=>{
  const {facts,manifest}=fresh().model_definitions;
  assert.equal(facts.products.length,10);
  assert.ok(facts.products.every(p=>p.functional_definition_proof.record.contract==='frozen-original-json-v1'));
  assert.ok(facts.products.every(p=>p.functional_definition_proof.record.canonical_json.includes('"synthetic_duration_seconds":0.125')));
  assert.ok(facts.products.some(p=>p.functional_supplier_proof.contract==='functional-supplier-scoped-proof-v2'));
  assert.equal(await validateFunctionalPacket(facts,manifest,{asOf:'2026-10-07T03:30:00Z'}),facts);
});

test('actual hybrid Fv4 supplier applications preserve company grouping and offer terms', async () => {
  const {facts,manifest}=fresh().scoped;
  const p=facts.products.find(p=>p.functional_supplier_proof.contract==='functional-supplier-scoped-proof-v2');
  assert.equal(p.supplier_leads_found,1);
  assert.equal(p.suppliers[0].product_offers.length,2);
  assert.equal(await validateFunctionalPacket(facts,manifest,{asOf:'2026-10-07T03:30:00Z'}),facts);
});

test('scoped supplier prices, MOQ, dates, origins and offer restrictions cannot be changed', async () => {
  for(const change of [p=>p.suppliers[0].product_offers[0].prices[0].amount='1',p=>p.suppliers[0].product_offers[0].min_order=1,
    p=>p.supplier_scoped_terms.all[0].terms[0].support_application_ids=[],p=>p.suppliers[0].current=false,
    p=>p.supplier_leads_found=3]){
    const {facts,manifest}=fresh().scoped,p=facts.products.find(p=>p.functional_supplier_proof.contract==='functional-supplier-scoped-proof-v2');
    change(p);await assert.rejects(validateFunctionalPacket(facts,manifest));
  }
});

test('actual scoped quoted ranges and conflicting required membership fees replay exactly', async () => {
  const {facts,manifest}=fresh().range;
  assert.equal(await validateFunctionalPacket(facts,manifest,{asOf:'2026-10-07T03:30:00Z'}),facts);
});

test('schema tuple cannot be downgraded or mixed with legacy workbook', async () => {
  for (const change of [v=>v.facts.schema_version=2,v=>v.manifest.schema_version=1,v=>v.manifest.workbook_contract='fit-checklist-v1']) {
    const f=fresh();change(f);await assert.rejects(validateFunctionalPacket(f.facts,f.manifest));
  }
});

test('original source and fit bytes cannot be changed while retaining the hash', async () => {
  for (const change of [p=>p.functional_fit_input.canonical_json+=' ',p=>p.example_products[0].observation.canonical_json+=' ']) {
    const f=fresh();change(f.facts.products[0]);await assert.rejects(validateFunctionalPacket(f.facts,f.manifest),/ORIGINAL_CHANGED/);
  }
});

test('displayed example prices and seller names must come from the original offer', async () => {
  for (const change of [p=>p.example_products[0].seller='Invented seller',p=>p.example_products[0].price={kind:'retail',displayed:'₹ 99',currency:'INR',basis:null,source_url:null}]) {
    const f=fresh();change(f.facts.products[0]);await assert.rejects(validateFunctionalPacket(f.facts,f.manifest),/EXAMPLE_DISPLAY_CHANGED/);
  }
});

test('fit explanation and copied scores must match their actual saved assessment', async () => {
  const f=fresh();f.facts.products[0].criteria[0].reason='Estimated: Invented improvement.';
  await assert.rejects(validateFunctionalPacket(f.facts,f.manifest),/FIT_ASSESSMENT_CHANGED/);
});

test('market displays and source counts cannot be inflated', async () => {
  const f=fresh();f.facts.products[0].market_checks[0].detail='Five hundred buyers';
  await assert.rejects(validateFunctionalPacket(f.facts,f.manifest),/MARKET_DISPLAY_CHANGED/);
});

test('full chosen evidence must remain current at the actual release time', async () => {
  const f=fresh();await assert.rejects(validateFunctionalPacket(f.facts,f.manifest,{asOf:'2026-10-13T03:30:00Z'}),/WITNESS_STALE/);
});

test('asset omission is legitimate but cannot be replaced by an undeclared photograph', async () => {
  const f=fresh();f.facts.products[0].display_asset={asset_id:'invented'};
  await assert.rejects(validateFunctionalPacket(f.facts,f.manifest));
});

test('outer packet still refuses fractional amounts outside original snapshots', () => {
  assert.throws(()=>canonical({invented_price:1.5}),/NONINTEGER_JSON/);
});

test('exact original decimal physical facts and a real parsed Trends series survive the cross-language reader', async () => {
  const {facts,manifest}=structuredClone(fixture.rich);
  await validateFunctionalPacket(facts,manifest,{asOf:'2026-10-07T03:30:00Z'});
  assert.ok(facts.products.some(p=>p.functional_fit_input.canonical_json.includes('0.35')));
  assert.ok(facts.products.some(p=>p.opportunity_basis.source_ids.includes('google-trends')));
});

test('accepted prose and both language input references are checked, not merely displayed text shape', async () => {
  for (const change of [f=>f.narratives.en.products[0].reason='The supplied facts make {{title}} perfect.',
    f=>f.narratives['hi-Latn'].writing_proof.items[Object.keys(f.narratives['hi-Latn'].writing_proof.items)[0]].reference.writing_input_hash='0'.repeat(64)]) {
    const f=fresh();change(f.facts);await assert.rejects(validateFunctionalPacket(f.facts,f.manifest),/WRITING/);
  }
});

test('actual original photo check and revision-owned display copy validate without shared files', async () => {
  const {facts,manifest}=structuredClone(fixture.photo);
  assert.ok(facts.products.some(p=>p.display_asset!==null));
  await validateFunctionalPacket(facts,manifest,{asOf:'2026-10-07T03:30:00Z'});
});

test('renewable photo generation freezes the actual physical response and original body hash', async () => {
  const {facts,manifest}=fresh().reviewed;
  const p=facts.products.find(p=>Object.values(p.functional_photo_proof.records.source_checks).some(r=>JSON.parse(r.canonical_json).version==='public-product-image-v3'));
  assert.ok(p);
  assert.equal(await validateFunctionalPacket(facts,manifest,{asOf:'2026-10-07T03:30:00Z'}),facts);
  const wrapper=Object.values(p.functional_photo_proof.records.source_checks).find(r=>JSON.parse(r.canonical_json).kind==='public-product-image-read-v2');
  const row=JSON.parse(wrapper.canonical_json);row.response_proof.body_sha256='0'.repeat(64);wrapper.canonical_json=canonical(row);wrapper.sha256=sha(encoder.encode(wrapper.canonical_json));
  await assert.rejects(validateFunctionalPacket(facts,manifest),/PHOTO_PROOF_INVALID|ORIGINAL_CHANGED/);
});

test('photo provenance cannot point to a different original or omit shared ownership proof', async () => {
  for(const change of [p=>p.image_provenance.source_url='https://images.example.org/not-the-source.png',
    p=>{const ids=Object.keys(p.functional_photo_proof.records.source_checks);delete p.functional_photo_proof.records.source_checks[ids.find(id=>id!==p.image_provenance.id)];}]) {
    const {facts,manifest}=structuredClone(fixture.photo), p=facts.products.find(p=>p.display_asset!==null);
    change(p);await assert.rejects(validateFunctionalPacket(facts,manifest),/PHOTO/);
  }
});

test('schema-3 private draft manifest uses the existing owner-only download route',async()=>{
  const packet=storedPacket(),bucket=fakeBucket(packet),request=new Request(`https://reports.example.com/admin/draft-files/${packet.manifest.issue_id}/1/shortlist.xlsx`,{headers:{'X-NeuraFlow-Release-Action':'v1'}});
  const response=await handleAdminRequest(request,environment(bucket),context,{clock:()=>new Date('2026-10-07T00:00:00Z')});
  assert.equal(response.status,200,await response.clone().text());assert.equal(response.headers.get('X-Content-SHA256'),packet.manifest.files[0].sha256);
  const denied=await handleAdminRequest(request,environment(bucket),{access:{...context.access,getIdentity:async()=>({email:'client@example.com'})}});
  assert.equal(denied.status,403);assert.ok(!bucket.calls.some(([action])=>action==='put'));
});

test('actual stored new tuple can activate through the unchanged owner-approved route',async()=>{
  const packet=storedPacket(),bucket=fakeBucket(packet);
  const response=await handleAdminRequest(activationRequest(packet,'2026-10-08T03:30:00Z'),environment(bucket),context,{clock:()=>new Date('2026-10-07T00:00:00Z')});
  assert.equal(response.status,200,await response.clone().text());assert.ok(bucket.calls.some(([action,key])=>action==='put'&&key==='clients/default/release-index.json'));
});

test('future schedule and time spent verifying files cannot activate expired type evidence',async()=>{
  const future=storedPacket('2026-10-13'),futureBucket=fakeBucket(future);
  const denied=await handleAdminRequest(activationRequest(future,'2026-10-13T03:30:00Z'),environment(futureBucket),context,{clock:()=>new Date('2026-10-07T00:00:00Z')});
  assert.equal(denied.status,409);assert.match(await denied.text(),/WITNESS_STALE/);assert.ok(!futureBucket.calls.some(([action])=>action==='put'));
  const current=storedPacket(),bucket=fakeBucket(current);let reads=0;
  const expired=await handleAdminRequest(activationRequest(current,'2026-10-08T03:30:00Z'),environment(bucket),context,{clock:()=>new Date(reads++===0?'2026-10-12T09:59:59Z':'2026-10-12T10:00:01Z')});
  assert.equal(expired.status,409);assert.match(await expired.text(),/WITNESS_STALE/);assert.ok(!bucket.calls.some(([action])=>action==='put'));
});

test('actual v4 source reviews and a later real blocked check keep their exact distinct outcomes',async()=>{
  const {facts,manifest}=structuredClone(fixture.reviewed);
  assert.ok(facts.products.some(p=>p.source_route_checks.some(r=>r.outcome==='completed_relevant'&&r.last_attempt?.outcome==='blocked')));
  await validateFunctionalPacket(facts,manifest,{asOf:'2026-10-07T03:30:00Z'});
});

test('route outcome text, counts and genuine last-attempt status are independently reconstructed',async()=>{
  for(const change of [r=>r.detail='The saved source is entirely confirmed.',r=>r.measurements[0].value+=1,
    r=>{r.outcome='completed_no_results';r.state='No usable result found';},r=>r.last_attempt.outcome='usable']){
    const {facts,manifest}=structuredClone(fixture.reviewed),p=facts.products.find(p=>p.source_route_checks.some(r=>r.last_attempt?.outcome==='blocked'));
    change(p.source_route_checks.find(r=>r.last_attempt?.outcome==='blocked'));
    await assert.rejects(validateFunctionalPacket(facts,manifest),/ROUTE/);
  }
});

test('dated supplier company context is reconstructed without inheriting purchase promises or product prices',async()=>{
  const f=structuredClone(fixture.reviewed),p=f.facts.products.find(p=>p.supplier_leads_found);
  assert.equal(p.supplier_leads_checked,2);assert.equal(p.suppliers.length,1);
  assert.equal(p.suppliers[0].single_piece_dispatch,'not_stated');assert.deepEqual(p.supplier_price_observations,[]);
  assert.equal(p.supplier_decisions.length,1);await validateFunctionalPacket(f.facts,f.manifest);
  for(const change of [p=>p.supplier_leads_checked++,p=>p.supplier_decisions=[],p=>p.suppliers[0].single_piece_dispatch='supported',p=>p.supplier_effort.internal_outcome='failed',p=>p.suppliers=[]]){
    const g=structuredClone(fixture.reviewed);change(g.facts.products.find(p=>p.supplier_leads_found));
    await assert.rejects(validateFunctionalPacket(g.facts,g.manifest),/SUPPLIER_DISPLAY_CHANGED|FACT_REQUIREMENTS_NOT_MET/);
  }
});

test('a genuine newer global listing cannot leave an older convenient market vote in the report',async()=>{
  const {facts,manifest}=structuredClone(fixture.replaced);
  await assert.rejects(validateFunctionalPacket(facts,manifest),/MARKET_CURRENTNESS_CHANGED/);
});

test('supplier search outcome and retired routes match the Python owner exactly',()=>{
  // Same-original reparses (newest parser wins, whatever the record IDs), a later
  // refusal that cannot erase leads, and "blocked" only when every original failed.
  const at='2026-10-14T08:00:00+00:00',later='2026-10-14T09:00:00+00:00';
  const row=(route,pass,reference,outcome,when=at)=>({route,pass_id:pass,reference,internal_outcome:outcome,at:when});
  const cases=[
    [],
    [['a',row('tradeindia','t','shared/t/capture-parser-4.json','interpretation_pending')],['b',row('google-maps','m','shared/m/capture-parser-5.json','blocked',later)]],
    [['z-stale',row('google-maps','m','shared/m/capture-parser-4.json','interpretation_pending')],['a-new',row('google-maps','m','shared/m/capture-parser-5.json','blocked')]],
    [['a-stale',row('google-maps','m','shared/m/capture-parser-4.json','interpretation_pending')],['z-new',row('google-maps','m','shared/m/capture-parser-5.json','blocked')]],
    [['zz',row('indiamart','i','supplier_routes/i/capture.json','interpretation_pending')],['aa',row('indiamart','i','supplier_routes/i/capture-parser-3-0123456789abcdef.json','failed')]],
    [['a',row('google-maps','m','x/capture-parser-5.json','failed')],['b',row('indiamart','i','x/capture-parser-5.json','blocked',later)],['c',row('tradeindia','t','x/capture-parser-5.json','prelaunch_failed')]],
    [['a',row('known-supplier-pages','p','pages/a.json','usable_leads')]],
    [['a',row('indiamart','i','x/capture-parser-5.json','partial')],['b',row('tradeindia','t','x/capture-parser-5.json','completed_empty')]],
    [['a',row('google-maps','m','x\\capture-parser-5.json','failed')],['b',row('google-maps','m','x/capture.json','partial',later)]],
  ];
  const python=spawnSync('.venv/bin/python',['-c',`
import json,sys
from product_radar.supplier_effort import RETIRED_SUPPLIER_ROUTES,_current_outcome
cases=json.load(sys.stdin)
print(json.dumps({'retired':sorted(RETIRED_SUPPLIER_ROUTES),'outcomes':[_current_outcome([tuple(r) for r in rows]) for rows in cases]}))
`],{encoding:'utf8',input:JSON.stringify(cases),env:{...process.env,PYTHONPATH:'src:tests'}});
  assert.equal(python.status,0,python.stderr);
  const expected=JSON.parse(python.stdout);
  assert.deepEqual([...retiredSupplierRoutes].sort(),expected.retired);
  const fail=code=>{throw new Error(code);};
  assert.deepEqual(cases.map(rows=>currentSupplierOutcome(rows,fail)),expected.outcomes);
  assert.deepEqual(expected.outcomes,['not_attempted','interpretation_pending','blocked','blocked','failed','blocked',
    'interpretation_pending','completed_empty','failed']);
});
