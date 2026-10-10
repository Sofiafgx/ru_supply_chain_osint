"""Audit local procurement; admit distributor edges only with identity/sector evidence."""
import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path
from eis_ftp_contracts import valid_inn, dump_record
from local_contract_evidence import filter_csv, parse_kad_html

TARGETS = ['1646019864','1804002498','2130094170','5018077598','6612001460','7719830028','6444009038','7726019325','7722488005','5243001887']

def run(source, database, output, catalog=None, kad_html=(), targets=TARGETS):
    out = output.parent/'tier2_audit'
    audit = filter_csv(source, out, set(targets))
    distributors = json.loads(catalog.read_text()) if catalog else []
    verified = {d['inn']:d for d in distributors if valid_inn(d.get('inn')) and d.get('registration_source_url') and d.get('sector_source_url') and d.get('sector') in ('electronics','radio_components','metrology')}
    candidates = [json.loads(line, parse_float=Decimal) for line in (out/'contracts.ndjson').read_text().splitlines() if line.strip()]
    edges = {}
    provenance = []
    for r in candidates:
        if r['customer_inn'] not in targets or r['supplier_inn'] not in verified:
            continue
        row = {k:r[k] for k in ('customer_inn','supplier_inn','contract_id','sign_date','price_rub')}
        row['subject_text'] = r['product_description']
        encoded = dump_record(row)
        edges[encoded] = row
        provenance.append(dict(record_sha256=hashlib.sha256(encoded.encode()).hexdigest(),supplier_evidence=verified[r['supplier_inn']],contract_source_audit=str((out/'provenance.ndjson').resolve())))
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(''.join(edges),encoding='utf-8')
    (out/'edge_provenance.ndjson').write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in provenance))
    pairs = {tuple(sorted((r['customer_inn'],r['supplier_inn']))) for r in edges.values()}
    cases=[]; kad_audit=[]
    for html in kad_html:
        parsed, evidence = parse_kad_html(html,pairs)
        cases.extend(c.model_dump(mode='json') for c in parsed)
        kad_audit.append(evidence)
    (out/'court_cases.ndjson').write_text(''.join(json.dumps(c,ensure_ascii=False)+'\n' for c in cases))
    (out/'kad_pair_queue.json').write_text(json.dumps([dict(party_a_inn=a,party_b_inn=b,status='needs_search_and_case_documents') for a,b in sorted(pairs)],ensure_ascii=False,indent=2)+'\n')
    unique = {}
    for r in edges.values():
        if r['contract_id'] is not None:
            unique[(r['customer_inn'],r['contract_id'])] = r['price_rub']
    total = sum((p for p in unique.values() if p is not None),Decimal(0))
    audit.update(target_inns=targets,invalid_target_checksum=[t for t in targets if not valid_inn(t)],
        verified_distributor_inns=sorted({r['supplier_inn'] for r in edges.values()}),unique_pairs=len(pairs),
        confirmed_edges=len(edges),confirmed_contract_total_rub=str(total),contracts_missing_price=sum(p is None for p in unique.values()),
        distributor_catalog=str(catalog.resolve()) if catalog else None,
        catalog_note='Supplier checksum alone does not establish registration or distributor sector; an evidenced catalog is required.',
        kad=dict(requested_pairs=len(pairs),cases=len(cases),inputs=kad_audit,status='no_identified_pairs_to_check' if not pairs else 'case_documents_required',complete=False),
        gaps=['Anonymous suppliers cannot be reconstructed from subject text','Foreign-exporter links are not established by aggregated Comtrade data'])
    if database:
        with sqlite3.connect(f'file:{database.resolve()}?mode=ro',uri=True) as db:
            placeholders=','.join('?' for _ in targets)
            rows=db.execute(f"SELECT c.contract_ref_id,c.customer_tax_id,p.tax_id FROM contracts c LEFT JOIN parties p ON p.contract_ref_id=c.contract_ref_id AND p.role='supplier' WHERE c.customer_tax_id IN ({placeholders})",targets).fetchall()
            audit['sqlite_cross_check']=dict(file=str(database.resolve()),source_records=db.execute('SELECT count(*) FROM contracts').fetchone()[0],target_contracts=len({r[0] for r in rows}),identified_supplier_links=sum(r[2] is not None for r in rows))
    (out/'coverage.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(audit,ensure_ascii=False))
    return audit

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--contracts',type=Path,default=Path('data/collection_20261008/exports/contracts.csv'))
    p.add_argument('--database',type=Path,default=Path('data/compliance_offline_20261009/processed/offline_contracts.sqlite'))
    p.add_argument('--output',type=Path,default=Path('outputs/tier2_contract_edges.ndjson'))
    p.add_argument('--distributors',type=Path,help='Evidenced registry: inn,sector,registration_source_url,sector_source_url')
    p.add_argument('--kad',nargs='*',type=Path,default=[])
    a=p.parse_args();run(a.contracts,a.database,a.output,a.distributors,a.kad)
if __name__=='__main__': main()
