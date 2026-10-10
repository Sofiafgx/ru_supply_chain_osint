"""Build a local contract index from an archived normalized JSONL/GZIP export."""
import argparse
import collections
import gzip
import hashlib
import json
import sqlite3
from pathlib import Path


def index_archive(archive, output):
    output.mkdir(parents=True, exist_ok=True)
    database = output/'offline_contracts.sqlite'
    if database.exists():
        raise ValueError('Use a new output directory to preserve the existing offline index')
    stats = collections.Counter()
    digest = hashlib.sha256()
    source = gzip.open(archive, 'rb') if archive.suffix == '.gz' else archive.open('rb')
    with source, sqlite3.connect(database) as db:
        db.executescript('''
            CREATE TABLE contracts(
                contract_ref_id TEXT PRIMARY KEY, contract_date TEXT, customer_tax_id TEXT,
                contract_amount_rub REAL, product_codes_json TEXT, title TEXT,
                source_url TEXT, input_file TEXT, input_sha256 TEXT,
                execution_status TEXT DEFAULT NULL
            );
            CREATE TABLE parties(contract_ref_id TEXT, tax_id TEXT, role TEXT,
                PRIMARY KEY(contract_ref_id,tax_id,role));
            CREATE INDEX parties_tax_id ON parties(tax_id);
        ''')
        for line_number, line in enumerate(source,1):
            digest.update(line)
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get('record_type') != 'contract' or not isinstance(record.get('data'),dict):
                raise ValueError(f'Expected normalized contract record at line {line_number}')
            d = record['data']
            identifier = record['record_id']
            db.execute('INSERT INTO contracts VALUES(?,?,?,?,?,?,?,?,?,NULL)',
                (identifier,d.get('sign_date'),d.get('customer_inn'),d.get('price_rub'),
                 json.dumps(d.get('product_codes') or [],ensure_ascii=False),d.get('title'),
                 record.get('source_url'),record.get('input_file'),record.get('input_sha256')))
            if d.get('customer_inn'):
                db.execute('INSERT INTO parties VALUES(?,?,?)',(identifier,d['customer_inn'],'customer'))
            for supplier in set(d.get('supplier_inns') or []):
                db.execute('INSERT INTO parties VALUES(?,?,?)',(identifier,supplier,'supplier'))
            stats['contracts'] += 1
            if not d.get('supplier_inns'):
                stats['missing_supplier_inn'] += 1
            if d.get('price_rub') is None:
                stats['missing_amount'] += 1
        integrity = db.execute('PRAGMA quick_check').fetchone()[0]
        stats['distinct_party_inns'] = db.execute('SELECT COUNT(DISTINCT tax_id) FROM parties').fetchone()[0]
        stats['party_contract_role_records'] = db.execute('SELECT COUNT(*) FROM parties').fetchone()[0]
        if integrity != 'ok':
            raise ValueError('SQLite integrity check failed')
    result = {**dict(stats),'archive':str(archive.resolve()),'decompressed_sha256':digest.hexdigest(),
              'database':str(database.resolve()),'sqlite_quick_check':integrity,'network_requests':0,
              'verified_execution_statuses':0,'note':'Source metadata only. No inferred execution statuses or counterparty enrichment.'}
    (output/'offline_processing_audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(index_archive(args.archive,args.output),ensure_ascii=False))


if __name__=='__main__':
    main()
