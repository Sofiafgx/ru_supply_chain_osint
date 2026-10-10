"""Independent, read-only quality measurements for the existing data."""
import csv, hashlib, json, sqlite3
from collections import Counter
from pathlib import Path
from datetime import datetime, timezone
from supplier_enrichment import valid_inn
from collection_cli import query_match
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'data/workspace_audit_20261009'
inventory=json.loads((OUT/'file_inventory.json').read_text())
hashes={str((ROOT/x['path']).resolve()):x['sha256'] for x in inventory}
def db(path):
    c=sqlite3.connect((ROOT/path).as_uri()+'?mode=ro',uri=True); c.row_factory=sqlite3.Row; c.execute('BEGIN'); return c
def save(name,x): (OUT/name).write_text(json.dumps(x,ensure_ascii=False,indent=2,default=str)+'\n')
def verify(path,expected):
    actual=hashes.get(str(Path(path).resolve()))
    if actual is None and Path(path).exists():actual=hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return 'ok' if actual==expected else 'missing' if not Path(path).exists() else 'hash_mismatch'
c=db('data/collection_20261008/collection.sqlite')
plan=json.loads((ROOT/'config/collection_plan.json').read_text()); stats=Counter(); identifiers=set(); stages=Counter(); samples=[]
records={}
for r in c.execute("SELECT json FROM records WHERE kind='contract'"):
    x=json.loads(r[0]); d=x['data']; records[x['record_id']]=x
    stats['contracts']+=1; matched=any(query_match(d,q) for q in plan['queries']); stats['matched']+=matched
    suppliers=d.get('supplier_inns') or []; missing=not suppliers
    stats['missing_supplier']+=missing; stats['matched_missing_supplier']+=matched and missing
    stats['missing_customer']+=not d.get('customer_inn');stats['missing_date']+=not d.get('sign_date')
    stats['missing_price']+=d.get('price_rub') is None;stats['nonpositive_price']+=d.get('price_rub') is not None and d['price_rub']<=0
    for inn in [d.get('customer_inn')]+suppliers:
        if inn:identifiers.add(inn)
    stats['missing_supplier_with_name']+=missing and bool(d.get('supplier_names'))
    stages[str(d.get('source_execution_stage'))]+=1
    for k in ('source_url','input_file','input_sha256','collected_at'):stats['missing_'+k]+=not x.get(k)
    if d.get('sign_date') and not plan['start_date']<=d['sign_date']<=plan['end_date']:stats['outside_period']+=1
    if x.get('input_file'): stats['record_origin_'+verify(x['input_file'],x.get('input_sha256'))]+=1
stats['unique_inns']=len(identifiers);stats['invalid_inns']=sum(not valid_inn(i) for i in identifiers)
raw=[]
for r in c.execute('SELECT * FROM responses'):
    raw.append({'job_id':r['job_id'],'path':r['path'],'verification':verify(r['path'],r['sha256'])})
quality={'counts':dict(stats),'execution_stages':dict(stages),'raw_responses':dict(Counter(r['verification'] for r in raw)),
         'jobs':[dict(r) for r in c.execute('SELECT source,kind,status,count(*) n FROM jobs GROUP BY source,kind,status')],
         'source_status':[dict(r) for r in c.execute('SELECT * FROM source_status')],
         'foreign_key_issues':[tuple(r) for r in c.execute('PRAGMA foreign_key_check')]}
save('contract_quality.json',quality);save('raw_integrity.json',raw)
# Re-run pagination audit on a separate SQLite backup; original report is preserved.
from collection_audit import audit
copy=OUT/'main_snapshot';copy.mkdir(exist_ok=True)
target=sqlite3.connect(copy/'collection.sqlite'); c.backup(target);target.close();c.close()
(copy/'plan.json').write_text(json.dumps(plan))
page_audit=audit(copy,True);save('pagination_audit.json',page_audit)
# Contract exports must represent the same set of IDs, not merely the same row count.
export_checks=[]
for p in (ROOT/'data/collection_20261008/exports').glob('*.jsonl'):
    ids=[]
    with p.open() as f:
        for line in f:
            x=json.loads(line)
            if x.get('record_type')=='contract':ids.append(x['record_id'])
    if ids:export_checks.append({'path':str(p.relative_to(ROOT)),'rows':len(ids),'unique_ids':len(set(ids)),
        'outside_database':len(set(ids)-set(records)),'missing_from_full_database':len(set(records)-set(ids))})
save('export_checks.json',export_checks)
c=db('data/telegram_public/archive.sqlite3')
channels=[dict(r) for r in c.execute('SELECT s.*,count(p.message_id) posts,min(p.date) oldest_date,max(p.date) newest_date FROM web_sources s LEFT JOIN web_posts p USING(username) GROUP BY s.username')]
telegram={'rows':c.execute('SELECT count(*) FROM web_posts').fetchone()[0], 'channels':channels,'quality':dict(c.execute("SELECT sum(date IS NULL),sum(json_extract(record_json,'$.text')=''),sum(json_extract(record_json,'$.preview_warning') IS NOT NULL) FROM web_posts").fetchone()) if False else {}}
row=c.execute("SELECT sum(date IS NULL),sum(json_extract(record_json,'$.text')=''),sum(json_extract(record_json,'$.preview_warning') IS NOT NULL) FROM web_posts").fetchone()
telegram['quality']=dict(zip(('missing_date','empty_text','preview_warning'),row))
telegram['progress_file_posts']=json.loads((ROOT/'data/telegram_public/progress.json').read_text())['posts']
for folder,table,file in [('telegram_structured','analyzed_posts','structured.sqlite3'),('telegram_filtered','filtered_posts','filtered.sqlite3')]:
    sub=db('data/'+folder+'/'+file)
    analyzed=sub.execute('select post_id,fingerprint from '+table).fetchall();source_ids={f"public_web:{r['username'].lower()}:{r['message_id']}" for r in c.execute('select username,message_id from web_posts')}
    telegram[folder]={'analyzed':len(analyzed),'not_yet_analyzed':len(source_ids-{r['post_id'] for r in analyzed}),'analyzed_not_in_source':sum(r['post_id'] not in source_ids for r in analyzed)}
    sub.close()
c.close();save('telegram_quality.json',telegram)
c=db('data/social_history/social.sqlite3')
social={'posts':c.execute('select count(*) from posts').fetchone()[0], 'in_period':c.execute("select count(*) from posts where timestamp>='2022-01-01T00:00:00.000000Z'").fetchone()[0],
        'by_account':[dict(r) for r in c.execute("select platform,account_handle,count(*) all_posts,sum(timestamp>='2022-01-01T00:00:00.000000Z') in_period,min(timestamp) oldest,max(timestamp) newest from posts group by platform,lower(account_handle)")],
        'all_source_modes':dict(c.execute("select json_extract(json,'$.source_mode'),count(*) from posts group by 1")),
        'date_id_conflicts':[]}
# X snowflake contains the ID's creation timestamp; compare with saved publication time.
for r in c.execute("select * from posts where platform='X'"):
    if r['post_id'].isdigit():
        implied=datetime.fromtimestamp(((int(r['post_id'])>>22)+1288834974657)/1000,timezone.utc)
        saved=datetime.fromisoformat(r['timestamp'].replace('Z','+00:00')); delta=abs((saved-implied).total_seconds())
        if delta>86400:social['date_id_conflicts'].append({'post_id':r['post_id'],'account':r['account_handle'],'saved_timestamp':r['timestamp'],'id_implied_utc':implied.isoformat(),'delta_days':delta/86400})
c.close();save('social_quality.json',social)
old=db('data/collection_20261008/collection.sqlite'); oldcases={r[0] for r in old.execute("select id from records where kind='case'")};old.close()
cases=[json.loads(line) for line in (ROOT/'data/compliance_offline_20261009/browser_cases.jsonl').read_text().splitlines() if line.strip()]
ids=[x['record_id'] for x in cases]
save('court_quality.json',{'browser_rows':len(cases),'browser_unique':len(set(ids)),'old_unique':len(oldcases),'overlap':len(oldcases&set(ids)),'union_unique':len(oldcases|set(ids)),
 'records_outside_period':sum(not '2022-01-01'<=x['data'].get('registration_date','')<='2026-10-08' for x in cases),
 'missing_dates':sum(not x['data'].get('registration_date') for x in cases),
 'raw_origin':dict(Counter(verify(x['input_file'],x['input_sha256']) if x.get('input_file') else 'missing_origin' for x in cases))})
notices=json.loads((ROOT/'data/industrial_20261009_followup/records.json').read_text());proof=json.loads((ROOT/'data/industrial_20261009_followup/records.evidence.json').read_text())
save('industrial_quality.json',{'records':len(notices),'unique_urls':len({x['source_url'] for x in notices}),
 'missing':{k:sum(x.get(k) is None for x in notices) for k in notices[0]},
 'activity_labels':dict(Counter(x['activity_status_proxy'] for x in notices)), 'by_source':dict(Counter(x['source'] for x in proof)),
 'raw_origin':dict(Counter(verify(x['input_file'],x['input_sha256']) if x.get('input_file') else 'missing_origin' for x in proof))})
print(json.dumps({'contracts':dict(stats),'telegram':telegram['rows'],'social_date_id_conflicts':len(social['date_id_conflicts']),'pagination_issues':len(page_audit['pagination_issues'])},ensure_ascii=False))
