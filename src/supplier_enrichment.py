"""Read-only supplier audit; name matches are candidates, never verified INNs."""
import argparse
import collections
import csv
import hashlib
import json
import re
import sqlite3
import unicodedata
from pathlib import Path


def valid_inn(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{10}|[0-9]{12}", value):
        return False
    digits = list(map(int, value))
    def check(weights):
        return sum(digit * weight for digit, weight in zip(digits, weights)) % 11 % 10
    if len(value) == 10:
        return check([2, 4, 10, 3, 5, 9, 4, 6, 8]) == digits[9]
    return (check([7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) == digits[10]
            and check([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) == digits[11])


def name_key(value):
    # Preserve legal form and every word. Even a unique match needs review.
    return ' '.join(re.sub(r'[^\w]+', ' ', unicodedata.normalize('NFKC', value).casefold()).split())


def supplier_names(row):
    names = list(row.get('supplier_names') or [])
    if row.get('supplier_name'):
        names.append(row['supplier_name'])
    for supplier in row.get('suppliers') or []:
        if isinstance(supplier, dict):
            names.extend(supplier[k] for k in ('name', 'fullName', 'organizationName') if supplier.get(k))
    return sorted({n.strip() for n in names if isinstance(n, str) and n.strip()})


def load_index(paths):
    index = collections.defaultdict(dict)
    stats = collections.Counter()
    for path in paths:
        with path.open(encoding='utf-8-sig', newline='') as handle:
            for number, row in enumerate(csv.DictReader(handle), 2):
                stats['directory_rows'] += 1
                tax_id = row.get('inn', '').strip()
                if not valid_inn(tax_id):
                    stats['directory_invalid_or_missing_inn'] += 1
                    continue
                names = json.loads(row['names']) if row.get('names') else [row.get('name', '')]
                for name in names:
                    if name and name_key(name):
                        index[name_key(name)].setdefault(tax_id, []).append(
                            {'file': str(path.resolve()), 'line': number, 'name': name,
                             'profile_url': row.get('profile_url'), 'match_method': row.get('match_method')})
    stats['indexed_names'] = len(index)
    return index, stats


def name_candidates(names, index):
    return [{'name': name, 'candidate_inns': sorted(index.get(name_key(name), {})),
             'status': ('unique_name_candidate' if len(index.get(name_key(name), {})) == 1
                        else 'ambiguous_name' if index.get(name_key(name)) else 'no_match'),
             'evidence': index.get(name_key(name), {})} for name in names]


def audit(archive, directories, output):
    output.mkdir(parents=True, exist_ok=True)
    index, stats = load_index(directories)
    missing = {}
    with sqlite3.connect(archive.resolve().as_uri() + '?mode=ro', uri=True) as db:
        for (blob,) in db.execute("SELECT json FROM records WHERE kind='contract'"):
            record = json.loads(blob)
            data = record['data']
            stats['contracts'] += 1
            if data.get('supplier_inns'):
                continue
            missing[record['record_id']] = {
                'contract_ref_id': record['record_id'], 'customer_tax_id': data.get('customer_inn'),
                'source_url': record.get('source_url'), 'supplier_names': supplier_names(data),
                'raw_evidence': [], 'raw_supplier_inns': set()}
        # Check every archived response, including duplicate observations of a contract.
        responses = db.execute("SELECT DISTINCT r.path, r.sha256 FROM responses r JOIN jobs j ON j.id=r.job_id WHERE j.source='clearspending'").fetchall()
    raw_manifest = []
    for filename, expected in responses:
        path = Path(filename)
        if not path.is_absolute():
            path = archive.parent / path
        try:
            content = path.read_bytes()
            actual = hashlib.sha256(content).hexdigest()
            if actual != expected:
                raise ValueError('SHA256 mismatch')
            payload = json.loads(content)
            rows = payload if isinstance(payload, list) else payload.get('data', [])
            if not isinstance(rows, list):
                raise ValueError('Unexpected raw response shape')
            for number, row in enumerate(rows):
                identifier = str(row.get('regnum') or row.get('regNum') or '')
                if identifier not in missing:
                    continue
                record = missing[identifier]
                names = supplier_names(row)
                tax_ids = list(row.get('supplier_inns') or []) + [row.get('supplier_inn')]
                tax_ids.extend(s.get('inn') for s in row.get('suppliers', []) if isinstance(s, dict))
                tax_ids = {i for i in tax_ids if valid_inn(i)}
                if names or tax_ids:
                    record['supplier_names'] = sorted(set(record['supplier_names']) | set(names))
                    record['raw_supplier_inns'].update(tax_ids)
                    record['raw_evidence'].append({'file': str(path.resolve()), 'sha256': actual, 'row': number})
            raw_manifest.append({'file': str(path.resolve()), 'sha256': actual, 'rows': len(rows)})
            stats['raw_files_verified'] += 1
        except (OSError, ValueError, TypeError) as error:
            stats['raw_files_failed'] += 1
            raw_manifest.append({'file': str(path), 'error': str(error)})
    with (output / 'missing_suppliers.jsonl').open('w') as handle:
        for record in missing.values():
            record['raw_supplier_inns'] = sorted(record['raw_supplier_inns'])
            record['name_candidates'] = name_candidates(record['supplier_names'], index)
            record['supplier_tax_id'] = None
            record['execution_status'] = None
            if record['raw_supplier_inns']:
                record['status'] = 'raw_identifier_recovered_requires_review'
            elif record['supplier_names']:
                record['status'] = 'name_resolution_requires_review'
            else:
                record['status'] = 'missing_supplier_identity'
            stats[record['status']] += 1
            handle.write(json.dumps(record, ensure_ascii=False) + '\n')
    stats['missing_supplier_inn'] = len(missing)
    stats['verified_inns_written'] = 0
    (output / 'enrichment_audit.json').write_text(json.dumps(dict(stats), ensure_ascii=False, indent=2) + '\n')
    (output / 'raw_manifest.json').write_text(json.dumps(raw_manifest, ensure_ascii=False, indent=2) + '\n')
    return dict(stats)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, default=Path('data/collection_20261008/collection.sqlite'))
    parser.add_argument('--directories', type=Path, nargs='+', default=[Path('data/imports/companies.csv'), Path('database/employers_2026-10-09.csv')])
    parser.add_argument('--output', type=Path, default=Path('data/compliance_remediation_20261009'))
    args = parser.parse_args()
    print(json.dumps(audit(args.archive, args.directories, args.output), ensure_ascii=False))


if __name__ == '__main__':
    main()
