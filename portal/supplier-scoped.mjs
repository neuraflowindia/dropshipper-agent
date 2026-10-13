// Saved-only scoped supplier reader. Every commercial field is rederived from
// the actual F request, literal saved pages, and dated application originals.
const VERSION = 'supplier-scoped-terms-v2';
const CONTRACT = 'functional-supplier-scoped-proof-v2';
const TABLES = ['concepts', 'source_checks', 'supplier_assessments', 'jobs'];
const FIELDS = ['subscription_requirement', 'recurring_fee_frequency', 'monthly_fee', 'purchase_moq',
  'single_piece_purchase', 'direct_customer_dispatch', 'bulk_stock_prerequisite', 'cod', 'india_coverage',
  'white_label', 'white_label_shipping', 'neutral_packaging', 'ecommerce_support', 'order_integration',
  'setup_fee', 'deposit', 'per_order_fee'];
const YESNO = ['single_piece_purchase', 'direct_customer_dispatch', 'bulk_stock_prerequisite', 'cod',
  'white_label', 'white_label_shipping', 'neutral_packaging', 'ecommerce_support', 'order_integration'];
const MEANING = ['primary_use', 'mechanism', 'use_context', 'compatibility_constraints', 'included_distinctions', 'excluded_distinctions'];
const copy = value => structuredClone(value);
const cmp = (a, b) => a < b ? -1 : a > b ? 1 : 0;
const WORKFLOW = 'Single-piece purchase and direct customer dispatch; subscription and fees must be stated separately.';

export async function scopedSuppliers(product, legacy, tools) {
  const {check, keys, same, deep, digest, key, recordMaps, rawHash, hash, canonical, utc, displayUrl,
    publicUrl, supplierEffortRecord, definition, model, text, integer, ID, HASH} = tools;
  const proof = product.functional_supplier_proof, at = utc(product.example_as_of);
  const verify = condition => check(condition, 'ADMIN_FUNCTIONAL_SCOPED_SUPPLIER_INVALID');
  verify(keys(proof, ['contract', 'as_of', 'concept_id', 'legacy_context', 'records']) && proof.contract === CONTRACT &&
    proof.as_of === product.example_as_of && proof.concept_id === product.concept_id && keys(proof.records, TABLES));
  const nodes = await recordMaps(proof.records, product.source_revisions);
  const state = Object.fromEntries(TABLES.map(t => [t, Object.fromEntries(Object.entries(nodes[t]).map(([id,n]) => [id,n.value]))]));
  const originalState='{'+[...TABLES].sort().map(t=>canonical(t)+':{'+Object.keys(nodes[t]).sort().map(id=>canonical(id)+':'+nodes[t][id].raw).join(',')+'}').join(',')+'}';
  verify(new TextEncoder().encode(originalState).length <= 2 * 1024 * 1024 &&
    same(state.concepts[product.concept_id]?.description, product.description) && Object.keys(state.concepts).length === 1);
  const definitions = Object.entries(state.source_checks).filter(([,r])=>r.kind==='functional-definition-v1');
  for (const [id,row] of definitions) { verify(row.concept_id===product.concept_id); await definition(nodes.source_checks[id],id); }
  definitions.sort((a,b)=>a[1].revision-b[1].revision);
  for(const [i,[,row]] of definitions.entries()) verify(row.revision===i+1 && row.previous_definition_id===(i?definitions[i-1][0]:null) && (!i||row.previous_meaning_hash===definitions[i-1][1].meaning_hash));
  verify(definitions.length && definitions.at(-1)[1].meaning_hash===product.functional_definition.meaning_hash);
  const used = Object.fromEntries(TABLES.map(t=>[t,new Set()]));
  used.concepts.add(product.concept_id); for(const [id] of definitions)used.source_checks.add(id);

  function pages(item) {
    verify(Array.isArray(item.pages) && item.pages.length>=1 && item.pages.length<=8);
    const map = Object.fromEntries(item.pages.map(p=>[p.id,p]));
    verify(Object.keys(map).length===item.pages.length);
    for(const p of item.pages)verify(keys(p,['id','url','title','text'])&&ID.test(p.id)&&typeof p.title==='string'&&p.title.length<=2000&&typeof p.text==='string'&&p.text.length<=24000&&publicUrl(p.url));
    return map;
  }
  function citations(row, map, required=false, restore=false) {
    verify(Array.isArray(row.references)&&new Set(row.references).size===row.references.length&&row.references.every(id=>map[id])&&Array.isArray(row.quotes)&&(!required||row.references.length&&row.quotes.length));
    for(const q of row.quotes){verify(keys(q,['page_id','excerpt'])&&row.references.includes(q.page_id)&&text(q.excerpt));
      const escaped=q.excerpt.trim().replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
      const start=/[\p{L}\p{N}_]/u.test(q.excerpt.trim()[0])?'(?<![\\p{L}\\p{N}_])(?<!\\d[.,])':'';
      const end=/\d$/.test(q.excerpt.trim())?'(?![\\p{L}\\p{N}_]|[.,]\\d)':/[\p{L}\p{N}_]$/u.test(q.excerpt.trim())?'(?![\\p{L}\\p{N}_])':/[\d][.,]$/.test(q.excerpt.trim())?'(?!\\d)':'';
      const flexible=q.excerpt.trim().split(/\s+/).map(s=>s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('\\s+');
      const found=new RegExp(start+escaped+end,'u').exec(map[q.page_id].text)??new RegExp(start+flexible+end,'u').exec(map[q.page_id].text);
      verify(found && (restore||found[0]===q.excerpt)); if(restore)q.excerpt=found[0];
    }
  }
  function quotes(values,map){verify(Array.isArray(values)&&values.length>=1&&values.length<=8);citations({references:[...new Set(values.map(q=>q.page_id))].sort(),quotes:values},map,true);}
  async function identity(value,map){
    if(value===null)return null;
    let basis;
    if(value.kind==='registered_identifier'){
      verify(keys(value,['kind','scheme','value','quotes']));
      const pattern={GSTIN:/^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]$/,CIN:/^[LU]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}$/}[value.scheme];
      verify(pattern?.test(value.value)); quotes(value.quotes,map);
      verify(value.quotes.some(q=>new RegExp('(?<!\\w)'+value.value+'(?!\\w)').test(q.excerpt)));
      basis={kind:value.kind,scheme:value.scheme,value:value.value};
    }else{
      verify(value.kind==='page_identity'&&keys(value,['kind','name','address','page_id','quotes'])&&text(value.name)&&value.name.length<=300&&text(value.address)&&value.address.length>=20&&value.address.trim().split(/\s+/).length>=4&&map[value.page_id]);quotes(value.quotes,map);
      verify(value.quotes.every(q=>q.page_id===value.page_id)&&value.quotes.some(q=>q.excerpt.includes(value.name)&&q.excerpt.includes(value.address)));
      basis={kind:value.kind,url:map[value.page_id].url,name:value.name,address:value.address};
    }
    return key('supplier-company',basis);
  }
  async function scopedItem(item){
    verify(keys(item,['id','scope','product','workflow','company_identity','offer_identity','scope_page_ids','pages'])&&
      keys(item.scope,['scope_kind','company_ref','workflow_ref','offer_ref','purchase_unit'])&&['company_workflow','product_offer','unclear'].includes(item.scope.scope_kind));
    const map=pages(item), selected=item.scope_page_ids;
    verify(Array.isArray(selected)&&selected.length&&new Set(selected).size===selected.length&&selected.every(p=>map[p]));
    const company=await identity(item.company_identity,map), workflow=company&&item.workflow?await key('supplier-workflow',[company,item.workflow]):null;
    verify((item.workflow===null||text(item.workflow)&&item.workflow.length<=500)&&item.scope.company_ref===company&&item.scope.workflow_ref===workflow);
    if(item.scope.scope_kind==='company_workflow')verify(item.product===null&&text(item.workflow)&&item.offer_identity===null);
    else verify(keys(item.product,['meaning_hash',...MEANING])&&item.product.meaning_hash===product.functional_definition.meaning_hash&&same(Object.fromEntries(MEANING.map(f=>[f,item.product[f]])),Object.fromEntries(MEANING.map(f=>[f,product.functional_definition[f]]))));
    let offer=null;
    if(item.scope.scope_kind==='product_offer'){
      const o=item.offer_identity;verify(keys(o,['page_id','label','purchase_unit','quotes'])&&selected.includes(o.page_id)&&text(o.label)&&o.label.length<=500&&(o.purchase_unit===null||text(o.purchase_unit)&&o.purchase_unit.length<=500));quotes(o.quotes,map);
      verify(o.quotes.every(q=>q.page_id===o.page_id)&&o.quotes.some(q=>q.excerpt.includes(o.label)&&(o.purchase_unit===null||q.excerpt.includes(o.purchase_unit))));
      offer=await key('supplier-offer',[map[o.page_id].url,o.label,o.purchase_unit]);verify(item.scope.purchase_unit===o.purchase_unit);
    }else verify(item.offer_identity===null&&item.scope.purchase_unit===null);
    verify(item.scope.offer_ref===offer&&item.id===await key('supplier-scope',[item.scope,item.product,selected.map(p=>map[p].url).sort()]));
    return map;
  }
  async function encoded(item,candidate=false){
    const refs=new Map([[item.id,(candidate?'candidate_':'scope_')+(await digest(item.id)).slice(0,16)]]);
    for(const page of item.pages)refs.set(page.id,'page_'+(await digest(page.id)).slice(0,16));
    if(candidate)for(const section of item.section_refs)refs.set(section.id,'section_'+(await digest(section.id)).slice(0,16));
    else for(const f of ['company_ref','workflow_ref','offer_ref'])if(item.scope[f]!==null)refs.set(item.scope[f],f.split('_')[0]+'_'+(await digest(item.scope[f])).slice(0,16));
    verify(new Set(refs.values()).size===refs.size);
    const names=candidate?['id','page_id','primary_page_id','section_ids','references']:['id','page_id','company_ref','workflow_ref','offer_ref','references','scope_page_ids'];
    function replace(v,f){if(typeof v==='string')return names.includes(f)?refs.get(v)??v:v;if(Array.isArray(v))return v.map(x=>replace(x,f));if(v&&typeof v==='object')return Object.fromEntries(Object.entries(v).map(([k,x])=>[k,replace(x,k)]));return v;}
    return replace(item);
  }
  async function job(semantic,item,candidate=false){
    const saved=state.jobs[semantic.job_id],j=saved?.request; used.jobs.add(semantic.job_id);
    verify(j&&j.role==='F'&&j.model===model&&j.prompt_version==='research-f-v4'&&semantic.job_id===await key('job',j));
    const payload={mode:candidate?'supplier_scope_candidates':'supplier_scoped_terms',supplier_contract:VERSION,supplier_fields:FIELDS,supplier_reference_scope:candidate?'short-scoped-candidates-v1':'short-scoped-ids-v2'};
    verify(same(Object.fromEntries(Object.entries(j.payload).filter(([k])=>k!=='items')),payload)&&j.payload.items.some(v=>same(v,item)));
    return j;
  }
  function result(item,value){
    const map=pages(item); verify(keys(value,['id','scope','relevance','terms','prices'])&&value.id===item.id&&same(value.scope,item.scope)&&keys(value.relevance,['relationship','references','reason'])&&['exact_product','relevant_company','unrelated','unclear'].includes(value.relevance.relationship)&&text(value.relevance.reason)&&value.relevance.references.every(id=>map[id]));
    if(['exact_product','relevant_company'].includes(value.relevance.relationship))verify(value.relevance.references.length);
    verify(Array.isArray(value.terms)&&same(value.terms.map(t=>t.field),FIELDS));
    for(const t of value.terms){verify(keys(t,['field','value','basis','references','quotes','reason','applicability','exceptions'])&&text(t.value)&&text(t.reason)&&['observed','inferred','not_stated','conflicting'].includes(t.basis)&&['applies','unclear','excluded'].includes(t.applicability)&&((t.basis==='not_stated')===(t.value==='Not stated'))&&((t.basis==='conflicting')===(t.value==='Conflicting')));citations(t,map,t.basis!=='not_stated');
      if(YESNO.includes(t.field))verify(['Yes','No','Not stated','Conflicting'].includes(t.value));if(t.field==='subscription_requirement')verify(['Required','Optional','Not required','Not stated','Conflicting'].includes(t.value));
      if([...YESNO,'subscription_requirement'].includes(t.field)&&!['Not stated','Conflicting'].includes(t.value))verify(t.basis==='observed');
      if(t.basis==='conflicting')verify(new Set(t.quotes.map(q=>canonical([q.page_id,q.excerpt.trim().replace(/\s+/g,' ')]))).size>=2);
      if(t.exceptions.length)quotes(t.exceptions,map);
      verify([...t.references,...t.exceptions.map(q=>q.page_id)].every(p=>item.scope_page_ids.includes(p)));
      if(t.field==='purchase_moq'&&t.basis!=='not_stated'){verify(item.scope.scope_kind==='product_offer');verify(t.quotes.some(q=>q.excerpt.includes(item.offer_identity.label)&&(item.scope.purchase_unit===null||q.excerpt.includes(item.scope.purchase_unit))));}
    }
    verify(Array.isArray(value.prices));
    for(const p of value.prices){citations(p,map,true);verify(text(p.reason)&&p.references.every(id=>item.scope_page_ids.includes(id))&&['product','mandatory_recurring','optional_recurring','setup','deposit','per_order'].includes(p.price_kind));
      const amount=p.amount??p.quoted_amount;verify(typeof amount==='string');
      const amounts=p.amount_range?[p.amount_range.minimum,p.amount_range.maximum]:[amount];
      for(const a of amounts){verify(/^\d+(?:\.\d{1,2})?$/.test(a)&&Number(a)<=1000000000);verify(p.quotes.some(q=>[...q.excerpt.matchAll(/(?<![\w.])\d[\d,]*(?:\.\d{1,2})?(?![\w.])/g)].some(n=>Number(n[0].replaceAll(',',''))===Number(a))));}
      if(p.amount_range)verify(p.amount===null&&Number(p.amount_range.minimum)<=Number(p.amount_range.maximum)&&p.quotes.some(q=>q.excerpt.includes(p.quoted_amount)));
      if(p.currency!==null)verify(({INR:['₹','INR','Rs','Rs.'],USD:['USD','US$'],EUR:['EUR','€']}[p.currency]??[]).some(m=>p.quotes.some(q=>q.excerpt.includes(m))));
      if(item.scope.scope_kind==='product_offer'){verify(value.relevance.relationship==='exact_product');const contextual=p.quotes.filter(q=>q.excerpt.includes(item.offer_identity.label)&&(item.scope.purchase_unit===null||q.excerpt.includes(item.scope.purchase_unit)));verify(contextual.length&&amounts.every(a=>contextual.some(q=>[...q.excerpt.matchAll(/(?<![\w.])\d[\d,]*(?:\.\d{1,2})?(?![\w.])/g)].some(n=>Number(n[0].replaceAll(',',''))===Number(a)))));}
      if(p.price_kind==='product')verify(item.scope.scope_kind==='product_offer'&&value.relevance.relationship==='exact_product');
    }
    verify(item.scope.scope_kind!=='company_workflow'||value.relevance.relationship!=='exact_product');
    return value;
  }
  async function candidate(review,id,semantic){
    verify(keys(review,['kind','input','input_hash','job_id','checked_at','result','record_hash'])&&review.kind==='supplier-candidate-review-v1'&&review.record_hash===await rawHash(nodes.supplier_assessments[id],['record_hash'])&&review.input_hash===await hash(nodes.supplier_assessments[id].children.input.raw));
    const input=review.input,map=pages(input),page=map[input.primary_page_id];verify(keys(input,['id','product','workflow','primary_page_id','section_refs','pages'])&&input.workflow===WORKFLOW&&page&&input.id===await key('supplier-candidate',[page.url,input.product,WORKFLOW]));
    const sections=[];for(let start=0;start<page.text.length;){let end=Math.min(page.text.length,start+6000);if(end<page.text.length){const split=Math.max(page.text.lastIndexOf('\n',end-1),page.text.lastIndexOf(' ',end-1));if(split>start)end=split+1;}const excerpt=page.text.slice(start,end);sections.push({id:await key('supplier-section',[page.url,start,end,excerpt]),page_id:page.id,start,end,excerpt});start=end;}
    verify(sections.length>=1&&sections.length<=8&&same(sections,input.section_refs));
    const j=await job(review,await encoded(input,true),true);verify(id===await key('supplier-candidate-review',[input,VERSION,model,'research-f-v4',j.runner]));
    verify(keys(review.result,['id','selections','reason'])&&review.result.id===input.id&&text(review.result.reason)&&Array.isArray(review.result.selections)&&review.result.selections.length<=8&&integer(semantic.selection_index,0,review.result.selections.length-1));
    const selection=review.result.selections[semantic.selection_index];verify(keys(selection,['section_ids','scope_kind','company_identity','offer_identity','relevance','terms','prices'])&&selection.section_ids.length&&new Set(selection.section_ids).size===selection.section_ids.length&&selection.section_ids.every(id=>sections.some(s=>s.id===id)));
    const identity=selection.company_identity, fields=['kind','scheme','value','name','address','page_id','quotes'];verify(keys(identity,fields));
    const needed={registered_identifier:['kind','scheme','value','quotes'],page_identity:['kind','name','address','page_id','quotes'],unknown:['kind','quotes']}[identity.kind];verify(needed&&fields.filter(f=>!needed.includes(f)).every(f=>identity[f]===null));
    const selectedIdentity=identity.kind==='unknown'?null:Object.fromEntries(needed.map(f=>[f,identity[f]]));if(identity.kind==='unknown')verify(!identity.quotes.length);
    const selectedPages=[...new Set([input.primary_page_id,...(selectedIdentity?.quotes??[]).map(q=>q.page_id)])].sort().map(id=>{const p=copy(map[id]);if(id!==input.primary_page_id)p.text=selectedIdentity.quotes.filter(q=>q.page_id===id).map(q=>q.excerpt).join('\n');return p;});
    if(selection.offer_identity)for(const q of selection.offer_identity.quotes)verify(selection.section_ids.some(id=>{const s=sections.find(v=>v.id===id);return s.page_id===q.page_id&&s.excerpt.trim().replace(/\s+/g,' ').includes(q.excerpt.trim().replace(/\s+/g,' '));}));
    const item=semantic.input;verify(same(item.pages,selectedPages)&&item.workflow===WORKFLOW&&same(item.company_identity,selectedIdentity)&&same(item.offer_identity,selection.offer_identity)&&same(item.scope_page_ids,[input.primary_page_id])&&item.scope.scope_kind===selection.scope_kind);
    // The scoped writer fills stable unanswered fields once; all returned
    // answers must remain the selected literal answers, not a second judgment.
    const selectedTerms=copy(selection.terms);for(const t of selectedTerms){citations(t,map,t.basis!=='not_stated',true);if(t.exceptions.length)citations({references:[...new Set(t.exceptions.map(q=>q.page_id))],quotes:t.exceptions},map,true,true);}
    const selectedPrices=copy(selection.prices);for(const p of selectedPrices){citations(p,map,true,true);const quoted=p.amount,range=/^((?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?)\s*[-–]\s*((?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?)$/.exec(quoted),number=v=>v.replaceAll(',','').replace(/^0+(?=\d)/,'');if(range){p.amount=null;p.quoted_amount=quoted;p.amount_range={minimum:number(range[1]),maximum:number(range[2])};}else p.amount=number(quoted);}
    const termMap=Object.fromEntries(selectedTerms.map(t=>[t.field,t]));
    for(const f of FIELDS)termMap[f]??={field:f,value:'Not stated',basis:'not_stated',references:[],quotes:[],reason:'No explicit term was returned for this field.',applicability:'unclear',exceptions:[]};
    const mandatory=selectedPrices.filter(p=>p.price_kind==='mandatory_recurring'),subscription=termMap.subscription_requirement;
    if(mandatory.length&&['Not stated','Not required','Optional'].includes(subscription.value)){
      const missing=subscription.value==='Not stated',extras=selectedTerms.find(t=>t.field==='subscription_requirement');
      termMap.subscription_requirement={field:'subscription_requirement',value:missing?'Required':'Conflicting',basis:missing?'observed':'conflicting',references:[...new Set([...(missing?[]:subscription.references),...mandatory.flatMap(p=>p.references)])].sort(),quotes:[...(missing?[]:subscription.quotes),...mandatory.flatMap(p=>p.quotes)],reason:missing?'An explicitly interpreted mandatory recurring fee establishes a required subscription.':'The interpreted fee and subscription statements conflict; both source statements are retained.',applicability:extras?.applicability??'applies',exceptions:extras?.exceptions??[]};
    }
    verify(same(semantic.result.relevance,selection.relevance)&&deep(semantic.result.prices,selectedPrices)&&deep(semantic.result.terms,FIELDS.map(f=>termMap[f])));
    verify(semantic.candidate_review_hash===await digest(review)&&semantic.job_id===review.job_id&&semantic.checked_at===review.checked_at&&semantic.id===undefined);
    return input;
  }

  const latest=new Map();
  for(const [aid,app] of Object.entries(state.source_checks)){
    if(app.kind!=='scoped-supplier-application-v1')continue;
    verify(keys(app,['kind','id','binding','binding_hash','prepared_at'])&&aid===app.id&&app.binding_hash===await digest(app.binding)&&aid===await key('supplier-application',app.binding));
    const b=app.binding;verify(keys(b,['assessment_id','assessment_hash','concept_id','meaning_hash','capture_id','capture_path','capture_request','capture_product','lead_id','original_item_id','effort_id','effort_hash','pages','input_hash'])&&b.concept_id===product.concept_id&&b.meaning_hash===product.functional_definition.meaning_hash);
    const semantic=state.supplier_assessments[b.assessment_id],node=nodes.supplier_assessments[b.assessment_id];verify(semantic&&semantic.kind===VERSION&&semantic.version===VERSION&&semantic.record_hash===await rawHash(node,['record_hash'])&&b.assessment_hash===node.hash&&semantic.input_hash===await hash(node.children.input.raw)&&b.input_hash===semantic.input_hash);
    const item=semantic.input;await scopedItem(item);result(item,semantic.result);
    verify(utc(semantic.checked_at)<=at&&utc(app.prepared_at)<=at);
    used.source_checks.add(aid);used.supplier_assessments.add(b.assessment_id);
    if(semantic.candidate_review_id){const rid=semantic.candidate_review_id;used.supplier_assessments.add(rid);await candidate(state.supplier_assessments[rid],rid,semantic);verify(b.assessment_id===await key('supplier-selected-scope',[rid,semantic.selection_index,item]));}
    else {const j=await job(semantic,await encoded(item));verify(b.assessment_id===await key('supplier-scoped',[item,VERSION,model,'research-f-v4',j.runner]));}
    const effort=state.source_checks[b.effort_id];verify(effort&&nodes.source_checks[b.effort_id].hash===b.effort_hash);await supplierEffortRecord(effort);used.source_checks.add(b.effort_id);
    const request=b.capture_request,cp=b.capture_product;verify((keys(request,['concept_id','lead'])||keys(request,['concept_id','lead','refresh']))&&request.concept_id===product.concept_id&&keys(request.lead,['id','urls'])&&request.lead.id===b.lead_id&&request.lead.urls.length>=1&&request.lead.urls.length<=3&&request.lead.urls.every(publicUrl)&&cp.concept_id===product.concept_id&&same(cp.description,product.description));
    verify(effort.pass_id===b.capture_id&&effort.concept_id===product.concept_id&&effort.reference===b.capture_path&&effort.description_hash===await digest(cp.description)&&b.capture_id===await key('supplier-capture',[cp,request.lead,...(request.refresh?[request.refresh]:[])])&&b.original_item_id==='supplier-item-'+(await digest([product.concept_id,b.lead_id])).slice(0,40));
    const map=pages(item);verify(Array.isArray(b.pages)&&b.pages.length===item.pages.length&&new Set(b.pages.map(p=>p.page_id)).size===b.pages.length);
    for(const p of b.pages){const receipt=effort.receipt_refs.find(r=>r.id===p.page_id);verify(keys(p,['page_id','url','content_hash','original_content_hash','receipt_hash','observed_at'])&&map[p.page_id]&&HASH.test(p.original_content_hash)&&p.content_hash===await digest(map[p.page_id])&&p.url===map[p.page_id].url&&receipt&&p.receipt_hash===receipt.hash&&p.observed_at===receipt.checked_at&&utc(p.observed_at)<=at);}
    const date=b.pages.map(p=>p.observed_at).sort((a,b)=>utc(a)-utc(b))[0],old=latest.get(item.id);
    if(!old||utc(date)>utc(old.date)||utc(date)===utc(old.date)&&aid>old.app.id)latest.set(item.id,{date,app,semantic});
  }
  for(const t of TABLES)verify(same(Object.keys(state[t]).sort(),[...used[t]].sort()));
  const projected=[];
  const expires = observed => {const fractional=/\.(\d+)(?:Z|\+00:00)$/.exec(observed)?.[1];return new Date(utc(observed)+30*86400000).toISOString().slice(0,19)+(fractional&&Number(fractional)?'.'+fractional.padEnd(6,'0'):'')+'+00:00';};
  const stamp = observed => ({observed_at:observed,valid_until:observed===null?null:expires(observed),current:observed!==null&&at>=utc(observed)&&at-utc(observed)<=30*86400000});
  for(const {app,semantic} of latest.values()){
    const b=app.binding,r=semantic.result,dates=Object.fromEntries(b.pages.map(p=>[p.page_id,p.observed_at])),scope=r.scope;
    const relevant=scope.scope_kind==='product_offer'?r.relevance.relationship==='exact_product':r.relevance.relationship==='relevant_company';
    const scoped=scope.scope_kind!=='unclear'&&(scope.scope_kind!=='company_workflow'||scope.workflow_ref!==null);
    const origin={application_id:app.id,assessment_id:b.assessment_id,support_application_ids:[app.id],support_assessment_ids:[b.assessment_id]};
    const terms=r.terms.map(t=>{const refs=[...t.references,...t.exceptions.map(q=>q.page_id)],date=refs.map(id=>dates[id]).sort((a,b)=>utc(a)-utc(b))[0]??null,s=stamp(date);return {...copy(t),...s,qualified:s.current&&relevant&&t.applicability==='applies'&&['observed','conflicting'].includes(t.basis)&&scoped,...scope,...origin};});
    const prices=r.prices.map(p=>{const s=stamp(p.references.map(id=>dates[id]).sort((a,b)=>utc(a)-utc(b))[0]);return {...copy(p),...s,qualified:s.current&&relevant&&scoped,...scope,...origin};});
    projected.push({id:semantic.input.id,...scope,application_id:app.id,assessment_id:b.assessment_id,lead_id:b.lead_id,relevance:copy(r.relevance),terms,prices:prices.filter(p=>p.price_kind==='product'),fees:prices.filter(p=>p.price_kind!=='product'),source_capture_at:Object.values(dates).sort((a,b)=>utc(a)-utc(b))[0],reviewed_at:semantic.checked_at,current:[...terms,...prices].some(r=>r.current),scoped_usable:[...terms,...prices].some(r=>r.qualified),page_refs:copy(b.pages)});
  }
  const order=(a,b)=>cmp(a.scope_kind,b.scope_kind)||cmp(a.company_ref??'',b.company_ref??'')||cmp(a.offer_ref??'',b.offer_ref??'')||cmp(a.id,b.id);projected.sort(order);
  for(const offer of projected){if(offer.scope_kind!=='product_offer')continue;
    const fields=Object.fromEntries(offer.terms.filter(t=>t.qualified).map(t=>[t.field,copy(t)])),blocks=new Set(offer.terms.filter(t=>t.basis!=='not_stated').map(t=>t.field));
    for(const c of projected.filter(r=>r.scope_kind==='company_workflow'&&r.company_ref!==null&&r.company_ref===offer.company_ref&&r.workflow_ref===offer.workflow_ref))for(const t of c.terms){if(!t.qualified||t.field==='purchase_moq'||blocks.has(t.field))continue;const old=fields[t.field];if(!old)fields[t.field]=copy(t);else if(old.value!==t.value)fields[t.field]={...old,value:'Conflicting',basis:'conflicting',references:[...new Set([...old.references,...t.references])].sort(),quotes:[...old.quotes,...t.quotes],exceptions:[...old.exceptions,...t.exceptions],observed_at:[old.observed_at,t.observed_at].sort((a,b)=>utc(a)-utc(b))[0],valid_until:[old.valid_until,t.valid_until].sort((a,b)=>utc(a)-utc(b))[0],support_application_ids:[...new Set([...old.support_application_ids,...t.support_application_ids])].sort(),support_assessment_ids:[...new Set([...old.support_assessment_ids,...t.support_assessment_ids])].sort(),reason:'Applicable company statements conflict; both were retained.'};}
    const minimum=/^(\d+)\s+(piece|pieces|unit|units|pack|packs)$/i.exec(fields.purchase_moq?.value??'');offer.min_order=minimum?Number(minimum[1]):null;offer.min_order_unit=minimum?minimum[2].toLowerCase():null;offer.effective_terms=FIELDS.filter(f=>fields[f]).map(f=>fields[f]);
    offer.support_application_ids=[...new Set([offer.application_id,...offer.effective_terms.flatMap(t=>t.support_application_ids)])].sort();offer.support_assessment_ids=[...new Set([offer.assessment_id,...offer.effective_terms.flatMap(t=>t.support_assessment_ids)])].sort();offer.scoped_usable=[...offer.effective_terms,...offer.prices,...offer.fees].some(t=>t.qualified);
    const stated=(f,v)=>fields[f]?.basis==='observed'&&fields[f]?.value===v,direct=stated('direct_customer_dispatch','Yes'),bulk=stated('bulk_stock_prerequisite','Yes'),single=stated('single_piece_purchase','Yes')||fields.purchase_moq?.basis==='observed'&&/^1\s+(piece|pieces|unit|units)$/i.test(fields.purchase_moq.value);
    offer.supply_arrangement=bulk&&direct?'Bulk stock purchase followed by individual dispatch':bulk?'Bulk supply; individual dispatch not established':single&&direct?'Single-piece purchase with direct customer dispatch':single?'Single-piece purchase; customer dispatch not stated':'Supply arrangement not stated';
    for(const p of offer.prices){const mk=minimum?(minimum[2].toLowerCase().startsWith('pack')?'pack':'piece'):null,pk=/\bpack/i.test(p.unit)?'pack':/\b(piece|unit)/i.test(p.unit)?'piece':null;p.moq=mk===pk?offer.min_order:null;p.moq_unit=offer.min_order_unit;}
  }
  const scoped={contract:VERSION,concept_id:product.concept_id,current:projected.filter(r=>r.current),dated:projected.filter(r=>!r.current),all:projected,application_ids:projected.map(r=>r.application_id).sort(),assessment_ids:[...new Set(projected.map(r=>r.assessment_id))].sort()};
  verify(same(scoped,product.supplier_scoped_terms));
  const groups=new Map();for(const r of projected){const id=await key('supplier-report-group',r.company_ref?['company',r.company_ref,r.workflow_ref]:['lead',r.lead_id]);if(!groups.has(id))groups.set(id,[]);groups.get(id).push(r);}
  const coverage=product.supplier_scope_coverage,groupIds=[...groups.keys()].sort();
  verify(keys(coverage,['available_groups','selected_groups','omitted_groups','selected_group_ids','omitted_group_ids','limit','record_byte_limit'])&&coverage.limit===20&&coverage.record_byte_limit===2*1024*1024&&same(coverage.selected_group_ids,groupIds)&&coverage.selected_groups===groupIds.length&&groupIds.length<=20&&Array.isArray(coverage.omitted_group_ids)&&same(coverage.omitted_group_ids,[...new Set(coverage.omitted_group_ids)].sort())&&coverage.omitted_group_ids.every(id=>ID.test(id)&&!groups.has(id))&&coverage.omitted_groups===coverage.omitted_group_ids.length&&coverage.available_groups===groupIds.length+coverage.omitted_groups);
  const displays=[],sourceRefs=[];
  for(const [id,rows] of [...groups].sort(([a],[b])=>cmp(a,b))){const useful=rows.filter(r=>['exact_product','relevant_company'].includes(r.relevance.relationship));if(!useful.length)continue;const company=[],offers=[];
    for(const row of rows){const item=state.supplier_assessments[row.assessment_id].input,value={...copy(row),scope_label:item.offer_identity?item.offer_identity.label:item.workflow||'Supplier information needs review',source_pages:item.pages.filter(p=>item.scope_page_ids.includes(p.id)).map(p=>({id:p.id,url:displayUrl(p.url),title:p.title}))};(row.scope_kind==='product_offer'?offers:company).push(value);for(const page of row.page_refs){const rid=await key('supplier-scoped-ref',[row.application_id,page.page_id]);if(!sourceRefs.some(r=>r.id===rid))sourceRefs.push({id:rid,url:displayUrl(page.url),source:'Supplier page (scoped review)',observed_at:page.observed_at,date_basis:'supplier_page_capture',original_page_id:page.page_id,application_id:row.application_id});}}
    const first=useful[0],item=state.supplier_assessments[first.assessment_id].input,page=item.pages.find(p=>item.scope_page_ids.includes(p.id))??item.pages[0],limitation="Company information and each offer's own terms are shown separately.";
    displays.push({id,lead_id:id,original_lead_ids:[...new Set(rows.map(r=>r.lead_id))].sort(),name:item.company_identity?.name??new URL(page.url).hostname??page.title,url:displayUrl(page.url),company_ref:first.company_ref,workflow_ref:first.workflow_ref,relationship:'relevant_company',relevance:{relationship:'relevant_company',reason:limitation,references:[]},single_piece_dispatch:'not_stated',subscription:'not_stated',min_order:null,ecommerce_support:'Not stated',reason:limitation,supply_arrangement:"See each offer's own purchase and dispatch terms.",terms:company.flatMap(r=>r.terms.filter(t=>t.qualified)),reviewed_at:rows.map(r=>r.reviewed_at).sort((a,b)=>utc(a)-utc(b)).at(-1),source_capture_at:rows.map(r=>r.source_capture_at).sort((a,b)=>utc(a)-utc(b))[0],current:rows.some(r=>r.current),scoped_usable:rows.some(r=>r.scoped_usable),company_workflows:company,product_offers:offers,scoped_supplier_contract:VERSION});
  }
  displays.sort((a,b)=>Number(!a.current)-Number(!b.current)||Number(!a.scoped_usable)-Number(!b.scoped_usable)||cmp(a.id,b.id));
  const expected=copy(legacy.expected);expected.suppliers=[...displays,...expected.suppliers];expected.supplier_leads_found=expected.suppliers.length;expected.supplier_leads_checked+=coverage.selected_groups;
  const actual=new Map(expected.supplier_effort.actual_attempt_refs.map(r=>[canonical([r.id,r.hash]),r])),checks=new Set(expected.supplier_effort.check_ids);
  for(const [id,r] of Object.entries(state.source_checks))if(r.kind==='supplier-effort-v1'&&utc(r.at)<=at){checks.add(id);for(const ref of r.receipt_refs)if(ref.proof!=='none'&&utc(ref.attempt_started_at)<=at&&utc(ref.checked_at)<=at)actual.set(canonical([ref.id,ref.hash]),{...ref,check_id:id,pass_id:r.pass_id});}
  const attempts=[...actual.values()].sort((a,b)=>utc(a.attempt_started_at)-utc(b.attempt_started_at)||cmp(a.id,b.id)||cmp(a.hash,b.hash));Object.assign(expected.supplier_effort,{check_ids:[...checks].sort(),actual_attempt_refs:attempts,last_attempt_at:attempts.map(r=>r.attempt_started_at).sort((a,b)=>utc(a)-utc(b)).at(-1)??null,effort_satisfied:Boolean(attempts.length),usable_lead_count:expected.suppliers.length,scoped_interpretation_ids:scoped.assessment_ids});
  if(expected.suppliers.length)expected.supplier_effort.internal_outcome='usable_leads';expected.supplier_effort_satisfied||=Boolean(attempts.length);if(scoped.application_ids.length)expected.supplier_search_status='complete';expected.supplier_search_check_ids=[...new Set([...expected.supplier_search_check_ids,...checks])].sort();if(displays.length)expected.supplier_limitation="Company information and each offer's own terms are shown separately.";
  verify(same(Object.fromEntries(Object.keys(expected).map(k=>[k,product[k]])),expected));
  return {legacy:legacy.accepted,source_refs:sourceRefs};
}
