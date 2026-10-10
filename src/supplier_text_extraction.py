"""Extract attributable INN mentions without assigning a contractual counterparty."""
import argparse
import collections
import hashlib
import json
import re
from pathlib import Path

from supplier_enrichment import valid_inn


TEXT_FIELDS = ('notice_title', 'contract_subject', 'supplier_clause', 'subject', 'title',
               'product_names', 'description', 'specification', 'specifications')
INN_MENTION = re.compile(r'\bИНН\s*(?:[:№=]|N)?\s*(\d{12}|\d{10})(?!\d)', re.IGNORECASE)
ROLES = {
    'subcontractor': r'\bсубподрядчик\w*|\bсубпоставщик\w*',
    'supplier': r'\bпоставщик\w*|\bисполнител\w*|\bподрядчик\w*',
    'customer': r'\bзаказчик\w*|\bпокупател\w*',
    'manufacturer': r'\bпроизводител\w*|\bизготовител\w*',
}


def text_values(value, path):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from text_values(item, f'{path}[{index}]')
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from text_values(item, f'{path}.{key}')


def mentions(text, customer_inn=None):
    result = []
    for match in INN_MENTION.finditer(text):
        tax_id = match.group(1)
        before = text[max(0, match.start()-200):match.start()]
        # The latest explicit role before the INN is a candidate association only.
        found = [(m.start(), role) for role, pattern in ROLES.items() for m in re.finditer(pattern, before, re.IGNORECASE)]
        role = max(found)[1] if found else 'unknown'
        if not valid_inn(tax_id):
            status = 'invalid_checksum'
        elif tax_id == customer_inn:
            status = 'customer_inn_mention'
        elif role == 'supplier':
            status = 'supplier_role_candidate'
        else:
            status = role + '_mention_requires_review'
        result.append({'mentioned_inn': tax_id, 'checksum_valid': valid_inn(tax_id),
                       'role_candidate': role, 'status': status,
                       'quote': text[max(0, match.start()-200):match.end()+200],
                       'offset_start': match.start(), 'offset_end': match.end(),
                       'supplier_tax_id': None, 'execution_status': None})
    return result


def scan(directory, output):
    output.mkdir(parents=True, exist_ok=True)
    missing = {json.loads(line)['contract_ref_id'] for line in (directory/'missing_suppliers.jsonl').open()}
    manifest = json.loads((directory/'raw_manifest.json').read_text())
    stats = collections.Counter()
    present = collections.Counter()
    seen = set()
    failures = []
    with (output/'text_inn_candidates.jsonl').open('w') as handle:
        for entry in manifest:
            path = Path(entry['file'])
            try:
                content = path.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                if digest != entry.get('sha256'):
                    raise ValueError('Unverified or changed raw file')
                payload = json.loads(content)
                rows = payload if isinstance(payload, list) else payload['data']
                for row_number, row in enumerate(rows):
                    identifier = str(row.get('regnum') or row.get('regNum') or '')
                    if identifier not in missing:
                        continue
                    seen.add(identifier)
                    stats['raw_missing_supplier_observations'] += 1
                    for field in TEXT_FIELDS:
                        if row.get(field) is not None:
                            present[field] += 1
                        for field_path, text in text_values(row.get(field), field):
                            stats['text_values_scanned'] += 1
                            for candidate in mentions(text, row.get('customer_inn')):
                                candidate.update(contract_ref_id=identifier, input_file=str(path.resolve()),
                                                 input_sha256=digest, raw_row=row_number, field_path=field_path)
                                stats[candidate['status']] += 1
                                stats['inn_mentions'] += 1
                                handle.write(json.dumps(candidate, ensure_ascii=False)+'\n')
                stats['raw_files_verified'] += 1
            except (OSError, ValueError, TypeError, KeyError) as error:
                failures.append({'file': str(path), 'error': str(error)})
    result = {**dict(stats), 'missing_supplier_contracts': len(missing),
              'unique_contracts_scanned': len(seen), 'raw_files_failed': len(failures),
              'inn_mentions': stats['inn_mentions'], 'verified_supplier_inns_written': 0,
              'field_presence_observations': dict(present),
              'requested_fields_absent': [f for f in ('notice_title', 'contract_subject', 'supplier_clause') if not present[f]],
              'failures': failures,
              'note': 'Regex candidates only. Repeated raw observations are retained with provenance. No counterparty or status is assigned.'}
    (output/'text_extraction_audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path('data/compliance_remediation_20261009'))
    parser.add_argument('--output', type=Path, default=Path('data/compliance_text_20261009'))
    args = parser.parse_args()
    print(json.dumps(scan(args.directory,args.output),ensure_ascii=False))


if __name__ == '__main__':
    main()
