"""Normalize explicitly labelled customs exports; retain uncertain fields in audit."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import mean, median, pstdev

FIELDS = ('importer_tax_id', 'exporter_name', 'hs_code', 'shipment_date',
          'quantity_units', 'net_weight_kg', 'declared_value_usd',
          'calculated_supply_interval_days')
PREFIXES = ('8542', '8526', '8457', '8458', '8459')


def text(value):
    if value is None:
        return None
    value = str(value).strip()
    return None if value.lower() in ('', '-', '—', '——', 'null', 'none', 'n/a') else value


def numeric(value):
    # Unit and currency conversions must occur in a documented source mapping.
    value = text(value)
    if value is None or not re.fullmatch(r'\d+(?:\.\d+)?', value):
        return None
    return float(value)


def scope_reasons(raw, plan):
    if not plan:
        return [], []
    excluded, pending = [], []
    identifier = text(raw.get('importer_tax_id'))
    if identifier is None:
        pending.append('missing_importer_tax_id')
    elif identifier not in plan['target_importer_inns']:
        excluded.append('importer_outside_target_list')
    elif len(identifier) != 10 or not identifier.isdigit() or (
        sum(int(x)*w for x, w in zip(identifier[:9], (2,4,10,3,5,9,4,6,8))) % 11 % 10 != int(identifier[-1])
    ):
        pending.append('invalid_importer_inn_checksum')
    country = text(raw.get('import_country'))
    if country is None:
        pending.append('missing_import_country')
    elif country != plan['target_country']:
        excluded.append('import_country_outside_scope')
    supplied_date = text(raw.get('shipment_date'))
    if raw.get('date_type') != 'declaration' or supplied_date is None:
        pending.append('unconfirmed_declaration_date')
    else:
        try:
            day = datetime.strptime(supplied_date.replace('/', '-'), '%Y-%m-%d').date().isoformat()
            if not plan['date_range']['start_date'] <= day <= plan['date_range']['end_date']:
                excluded.append('date_outside_scope')
        except ValueError:
            pending.append('invalid_declaration_date')
    return excluded, pending


def normalize(rows, plan=None):
    records, audit, seen = [], [], set()
    for index, raw in enumerate(rows, 1):
        excluded, pending = scope_reasons(raw, plan)
        if excluded or pending:
            audit.append({'input_row': index, 'status': 'excluded_scope' if excluded else 'pending_scope',
                          'reasons': excluded + pending, 'raw': raw})
            continue
        code = re.sub(r'[.\s]', '', text(raw.get('hs_code')) or '')
        prefixes = tuple(plan['hs_codes']) if plan else PREFIXES
        if not re.fullmatch(r'\d{6,12}', code) or not code.startswith(prefixes):
            audit.append({'input_row': index, 'status': 'excluded_hs', 'raw': raw})
            continue
        # National extensions are retained in audit; only HS6 is internationally comparable.
        by_importer_hs = bool(plan and plan.get('settings', {}).get('interval_grouping') == 'importer_hs6')
        normalized_code = code[:6] if by_importer_hs else (code if len(code) in (6, 8) else code[:6])
        warnings = []
        declaration_date = None
        observed_date = text(raw.get('shipment_date') or raw.get('observed_date'))
        if raw.get('date_type') == 'declaration' and observed_date:
            try:
                declaration_date = datetime.strptime(observed_date.replace('/', '-'), '%Y-%m-%d').date().isoformat()
            except ValueError:
                warnings.append('invalid_declaration_date')
        else:
            warnings.append('date_not_confirmed_as_declaration')
        quantity = numeric(raw.get('quantity_units'))
        if quantity is None and text(raw.get('quantity_unit')) in ('pcs', 'pieces', 'шт', 'шт.'):
            quantity = numeric(raw.get('quantity'))
        value = numeric(raw.get('declared_value_usd'))
        if raw.get('value_type') != 'customs_declared' or raw.get('value_currency') != 'USD':
            value = None
        row = dict(zip(FIELDS, (text(raw.get('importer_tax_id')), text(raw.get('exporter_name')),
            normalized_code, declaration_date, quantity, numeric(raw.get('net_weight_kg')), value, None)))
        warnings.extend('missing_' + k for k, v in row.items() if v is None and k != FIELDS[-1])
        source = text(raw.get('source'))
        identity = json.dumps({'source': source, 'raw': raw}, sort_keys=True, ensure_ascii=False)
        # Only identical source rows are removed. Similar rows may be distinct line items.
        digest = hashlib.sha256(identity.encode()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        records.append(row)
        audit.append({'input_row': index, 'output_row': len(records), 'record_id': digest,
                      'source': source, 'source_url': raw.get('source_url'),
                      'status': 'normalized_partial' if warnings else 'normalized',
                      'warnings': warnings, 'raw': raw})
    groups = defaultdict(list)
    for item in audit:
        if 'output_row' not in item:
            continue
        row = records[item['output_row'] - 1]
        by_importer_hs = bool(plan and plan.get('settings', {}).get('interval_grouping') == 'importer_hs6')
        required = ('importer_tax_id', 'hs_code', 'shipment_date') if by_importer_hs else ('importer_tax_id', 'exporter_name', 'hs_code', 'shipment_date')
        if all(row[k] for k in required) and item['source']:
            groups[(item['source'], row['importer_tax_id'], None if by_importer_hs else row['exporter_name'], row['hs_code'])].append(row)
    summaries = []
    for key, group in sorted(groups.items()):
        days = sorted({row['shipment_date'] for row in group})
        gaps = {b: (date.fromisoformat(b) - date.fromisoformat(a)).days for a, b in zip(days, days[1:])}
        for row in group:
            row[FIELDS[-1]] = gaps.get(row['shipment_date'])
        values = list(gaps.values())
        summaries.append(dict(zip(('source', 'importer_tax_id', 'exporter_name', 'hs_code'), key),
            distinct_declaration_days=len(days), interval_count=len(values),
            mean_days=mean(values) if values else None, median_days=median(values) if values else None,
            coefficient_of_variation=pstdev(values)/mean(values) if len(values) >= 2 else None,
            coverage='observed_only'))
    return records, audit, summaries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path, help='JSON array or UTF-8 CSV with explicitly mapped fields')
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--plan', type=Path, help='Target country, declaration dates and exact importer INNs')
    args = parser.parse_args()
    content = args.input.read_text(encoding='utf-8-sig')
    rows = list(csv.DictReader(content.splitlines())) if args.input.suffix.lower() == '.csv' else json.loads(content)
    if not isinstance(rows, list) or any(not isinstance(x, dict) for x in rows):
        raise ValueError('Expected an array of row objects')
    plan = json.loads(args.plan.read_text()) if args.plan else None
    records, audit, summaries = normalize(rows, plan)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, data in [('shipments.json', records), ('row_audit.json', audit), ('rhythm_summary.json', summaries)]:
        (args.output_dir / name).write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')
    with (args.output_dir / 'shipments.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(records)
    print(json.dumps({'normalized_rows': len(records), 'interval_groups': len(summaries),
                      'calculated_intervals': sum(r[FIELDS[-1]] is not None for r in records)}))


if __name__ == '__main__':
    main()
