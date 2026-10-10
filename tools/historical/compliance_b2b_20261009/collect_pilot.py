import concurrent.futures, datetime, hashlib, json, pathlib, re
import requests
ROOT = pathlib.Path(__file__).resolve().parents[3] / 'data/compliance_b2b_20261009'
QUERIES = [('metalworking', 'Станок фрезерный металлообрабатывающий', ('28.41.', '25.62.')), ('electronics', 'Схемы интегральные электронные', ('26.11.', '26.12.'))]

def valid_inn(v):
    if not isinstance(v,str) or not re.fullmatch(r'\d{10}',v): return False
    return sum(int(v[i])*w for i,w in enumerate([2,4,10,3,5,9,4,6,8]))%11%10 == int(v[-1])

def fetch(q):
    category,term,prefixes=q
    response=requests.get('https://clearspending.ru/api/gw/filtered-contracts/',params={'search':term,'page':1,'page_size':50},timeout=30)
    path=ROOT/'raw'/f'{category}.json'
    path.write_bytes(response.content)
    meta={'category':category,'request_url':response.url,'http_status':response.status_code,'input_file':str(path),'input_sha256':hashlib.sha256(response.content).hexdigest(),'collected_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
    try:
        response.raise_for_status(); payload=response.json()
        if not isinstance(payload.get('data'),list): raise ValueError('Unexpected response')
        meta.update(reported_count=payload.get('count'),reported_total_pages=payload.get('total_pages'),received_rows=len(payload['data']),pages_collected=1)
        return q,payload['data'],meta
    except Exception as e:
        meta['error']=str(e); return q,[],meta

records=[]; evidence=[]; coverage=[]; seen=set()
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    fetched=list(pool.map(fetch,QUERIES))
for (category,term,prefixes),rows,meta in fetched:
    accepted=0
    for r in rows:
        suppliers=r.get('supplier_inns') or []
        codes=list(dict.fromkeys(c for c in r.get('product_codes',[]) if c and c.startswith(prefixes)))
        if len(suppliers)!=1 or not valid_inn(r.get('customer_inn')) or not valid_inn(suppliers[0]) or len(codes)!=1: continue
        if not r.get('regnum') or r['regnum'] in seen or r.get('amount_rur') is None: continue
        try: d=datetime.date.fromisoformat(r['sign_date'])
        except (ValueError,TypeError,KeyError): continue
        if not datetime.date(2022,1,1)<=d<=datetime.date(2026,10,9): continue
        seen.add(r['regnum'])
        records.append({'customer_tax_id':r['customer_inn'],'supplier_tax_id':suppliers[0],'contract_ref_id':r['regnum'],'contract_date':r['sign_date'],'contract_amount_rub':r['amount_rur'],'product_category_code':codes[0],'execution_status':None,'source_url':f"https://clearspending.ru/contracts/{r['fz']}/{r['regnum']}"})
        evidence.append(dict(meta,contract_ref_id=r['regnum'],customer_name=r.get('customer_name'),supplier_names=r.get('supplier_names'),product_names=r.get('product_names'),source_current_stage=r.get('current_stage'),code_system='OKPD2',evidence_level='aggregator_contract_record',execution_status_reason='Source does not establish executed/litigated/breached',contract_ref_id_kind='registry_number',independent_primary_verification=False))
        accepted+=1
        if accepted==5: break
    meta['selected_records']=accepted; coverage.append(meta)

def save(name,value): (ROOT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
save('relations_pilot.json',records)
save('evidence.json',evidence)
save('coverage.json',coverage)
save('relations_strict.json',[])
schema={'$schema':'https://json-schema.org/draft/2020-12/schema','type':'array','items':{'type':'object','additionalProperties':False,'required':['customer_tax_id','supplier_tax_id','contract_ref_id','contract_date','contract_amount_rub','product_category_code','execution_status','source_url'],'properties':{'customer_tax_id':{'type':'string','pattern':'^[0-9]{10}([0-9]{2})?$'},'supplier_tax_id':{'type':'string','pattern':'^[0-9]{10}([0-9]{2})?$'},'contract_ref_id':{'type':'string','minLength':1},'contract_date':{'type':'string','format':'date'},'contract_amount_rub':{'type':'number'},'product_category_code':{'type':'string','minLength':1},'execution_status':{'type':['string','null'],'enum':['executed','litigated','breached',None]},'source_url':{'type':'string','format':'uri'}}}}
save('pilot.schema.json',schema)
assert len(records)<=10 and len({r['contract_ref_id'] for r in records})==len(records)
for r in records:
    assert set(r)==set(schema['items']['required']) and valid_inn(r['customer_tax_id']) and valid_inn(r['supplier_tax_id'])
    assert isinstance(r['contract_amount_rub'],(int,float)) and r['execution_status'] is None
for e in evidence:
    assert hashlib.sha256(pathlib.Path(e['input_file']).read_bytes()).hexdigest()==e['input_sha256']
print(json.dumps({'selected':len(records),'coverage':coverage},ensure_ascii=False))
