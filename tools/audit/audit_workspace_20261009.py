"""Read-only workspace audit; writes results to a separate audit directory."""
import ast, csv, hashlib, json, sqlite3, time, zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'data/workspace_audit_20261009'
OUT.mkdir(exist_ok=True)
def rel(p): return str(p.relative_to(ROOT))
def digest(p):
    h = hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''): h.update(chunk)
    return h.hexdigest()
def save(name, data): (OUT/name).write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
files = [p for p in ROOT.rglob('*') if p.is_file() and not any(s in p.parts for s in ('.venv','.git','__pycache__','workspace_audit_20261009'))]
inventory=[]
for p in files:
    inventory.append({'path':rel(p),'bytes':p.stat().st_size,'sha256':None if p.name in ('.env','config/private/proxies.txt') or p.name.endswith(('-wal','-shm')) else digest(p)})
save('file_inventory.json',inventory)
code=[]
for p in files:
    if p.suffix!='.py': continue
    src=p.read_text(); row={'path':rel(p),'lines':len(src.splitlines())}
    try:
        tree=ast.parse(src); row['syntax']='ok'
        row['definitions']=[{'name':n.name,'line':n.lineno,'type':type(n).__name__} for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))]
        row['imports']=[ast.unparse(n) for n in ast.walk(tree) if isinstance(n,(ast.Import,ast.ImportFrom))]
    except Exception as e: row['syntax']=str(e)
    code.append(row)
save('code_inventory.json',code)
dbs=[]
for p in files:
    if p.suffix not in ('.sqlite','.sqlite3','.db'): continue
    row={'path':rel(p),'started_utc':datetime.now(timezone.utc).isoformat()}
    try:
        c=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True); c.row_factory=sqlite3.Row; c.execute('BEGIN')
        row['quick_check']=[r[0] for r in c.execute('PRAGMA quick_check')]
        row['tables']={}
        for t,sql in c.execute("SELECT name,sql FROM sqlite_master WHERE type='table'").fetchall():
            if t.startswith('sqlite_'):continue
            tq='"'+t.replace('"','""')+'"'; cols=[r[1] for r in c.execute('PRAGMA table_info('+tq+')')]
            stats={'rows':c.execute('SELECT count(*) FROM '+tq).fetchone()[0],'columns':cols,'schema':sql}
            for col in ('status','source','kind','decision','username','platform'):
                if col in cols:
                    cq='"'+col+'"'; stats['by_'+col]={str(r[0]):r[1] for r in c.execute('SELECT '+cq+',count(*) FROM '+tq+' GROUP BY '+cq)}
            row['tables'][t]=stats
        c.rollback(); c.close()
    except Exception as e:row['error']=str(e)
    dbs.append(row)
save('database_inventory.json',dbs)
csvs=[]; csv.field_size_limit(10000000)
for p in files:
    if p.suffix!='.csv': continue
    row={'path':rel(p)}
    try:
        with p.open(newline='',encoding='utf-8-sig') as f:
            r=csv.DictReader(f); row['columns']=r.fieldnames; n=0; missing=Counter(); malformed=0
            for x in r:
                n+=1
                if None in x or any(v is None for v in x.values()):malformed+=1
                for k,v in x.items():
                    if k is not None and (v is None or not str(v).strip()): missing[k]+=1
            row.update(rows=n,missing=dict(missing),malformed=malformed)
    except Exception as e: row['error']=str(e)
    csvs.append(row)
save('csv_inventory.json',csvs)
jsons=[]
for p in files:
    if p.suffix not in ('.json','.jsonl','.ndjson'): continue
    row={'path':rel(p)}
    try:
        if p.suffix=='.json':
            x=json.loads(p.read_text()); row.update(type=type(x).__name__,items=len(x) if isinstance(x,(list,dict)) else None)
            if isinstance(x,dict):row['keys']=list(x.keys())
        else:
            n=0
            with p.open() as f:
                for line in f:
                    if line.strip():json.loads(line); n+=1
            row.update(type='lines',items=n)
    except Exception as e:row['error']=str(e)
    jsons.append(row)
save('json_inventory.json',jsons)
xlsx=[]; ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
for p in files:
    if p.suffix!='.xlsx':continue
    row={'path':rel(p),'sheets':[]}
    try:
        with zipfile.ZipFile(p) as z:
            shared=[]
            if 'xl/sharedStrings.xml' in z.namelist():
                shared=[''.join(si.itertext()) for si in ET.fromstring(z.read('xl/sharedStrings.xml'))]
            names=[s.attrib['name'] for s in ET.fromstring(z.read('xl/workbook.xml')).findall('m:sheets/m:sheet',ns)]
            for i,name in enumerate(names,1):
                tree=ET.fromstring(z.read(f'xl/worksheets/sheet{i}.xml')); rows=tree.findall('m:sheetData/m:row',ns); cells=tree.findall('.//m:c',ns)
                vals=[]
                for r in rows[:3]:
                    rv=[]
                    for cell in r:
                        v=cell.find('m:v',ns); value=v.text if v is not None else ''.join(cell.find('m:is',ns).itertext()) if cell.find('m:is',ns) is not None else None
                        if cell.attrib.get('t')=='s' and value is not None:value=shared[int(value)]
                        rv.append(value)
                    vals.append(rv)
                row['sheets'].append({'name':name,'xml_rows':len(rows),'cells':len(cells),'formula_cells':sum(c.find('m:f',ns) is not None for c in cells),'error_cells':sum(c.attrib.get('t')=='e' for c in cells),'first_rows':vals})
    except Exception as e:row['error']=str(e)
    xlsx.append(row)
save('xlsx_inventory.json',xlsx)
save('summary.json',{'generated_utc':datetime.now(timezone.utc).isoformat(),'files':len(files),'bytes':sum(x['bytes'] for x in inventory),'python_files':len(code),'python_lines':sum(x['lines'] for x in code),'syntax_errors':sum(x['syntax']!='ok' for x in code),'databases':len(dbs),'csv_files':len(csvs),'json_files':len(jsons),'json_errors':sum('error' in x for x in jsons),'xlsx_files':len(xlsx),'excluded':'.venv, .git, __pycache__, this audit output; secret files content not read'})
print(json.dumps(json.loads((OUT/'summary.json').read_text()),ensure_ascii=False))
