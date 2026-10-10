"""Fail-closed calculation boundary. Source archives are never modified."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

PATH_KEYS = {'input_file', 'source_file', 'source_path', 'input_path', 'path', 'run_id', 'run_name'}
QUANTITY = {'quantity', 'units_qty', 'units_per_system', 'r_kj', 'consumption_rate',
            'consumption_norm', 'specific_consumption', 'per_channel', 'value'}
STOCK = {'S_safe', 'D_daily', 'stock', 'daily_demand'}
CAPACITY = {'capacity', 'base_capacity', 'base_monthly_capacity', 'monthly_capacity',
            'production_capacity', 'output_capacity', 'annual_capacity', 'capacity_per_month'}
METRICS = {'tts', 'tts_days', 'tts_months', 'ttr', 'ttr_days', 'ttr_months',
           'rei', 'rei_interval', 'loss_interval', 'monthly_loss', 'daily_loss', 'max_loss'}
NORMS = QUANTITY - {'value', 'quantity', 'units_qty'}
NORMS |= {'calculated_component_multiplier', 'consumption_per_unit', 'quantity_per_system', 'units_per_weapon'}
NONFACTUAL = {'synthetic', 'demo', 'mock', 'test', 'scenario', 'observation_candidate', 'candidate'}


def excluded_path(value):
    if not isinstance(value, (str, Path)):
        return False
    parts = str(value).replace('\\', '/').lower().split('/')
    return any('synthetic' in p or 'smoke' in p or p in {'tests', 'test', 'examples', 'fixtures', 'mocks'}
               or p.startswith('demo_') or p.startswith('mock_') or p.endswith('_demo.jsonl') for p in parts)


def objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def synthetic_record(row, path='', metadata=None):
    for obj in objects([row, metadata or {}]):
        if any(obj.get(k) is True for k in ('is_synthetic', 'is_demo', 'is_mock', 'is_test')):
            return True
        if obj.get('evidence_class') in {'synthetic', 'demo', 'mock', 'test'}:
            return True
        if any(excluded_path(v) for k, v in obj.items() if k in PATH_KEYS):
            return True
    return excluded_path(path)


def public_url(value):
    if not isinstance(value, str):
        return False
    try:
        u = urlsplit(value)
        return u.scheme in ('https', 'http') and bool(u.hostname) and not u.username and not u.password
    except ValueError:
        return False


def document_evidence(obj):
    """A source URL alone is not a claim confirmation."""
    return isinstance(obj, dict) and public_url(obj.get('source_url')) and any(
        isinstance(obj.get(k), str) and obj[k].strip() for k in
        ('quote', 'evidence_excerpt', 'raw_quote', 'raw_snippet_quote', 'document_id',
         'contract_id', 'contract_number', 'case_number', 'patent_number', 'vri_id'))


def confirmed(obj):
    return isinstance(obj, dict) and obj.get('verified') is True and document_evidence(obj) and not synthetic_record(obj)


def timestamp(value):
    try:
        return isinstance(value, str) and datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is not None
    except ValueError:
        return False


def field_evidence(obj, key, value):
    evidence = obj.get('field_evidence', {}).get(key) if isinstance(obj.get('field_evidence'), dict) else None
    if not confirmed(evidence) or evidence.get('value') != value:
        return False
    if key.lower() in CAPACITY:
        return (evidence.get('method') == 'bottom_up_bom' and bool(evidence.get('inputs'))
                and isinstance(evidence['inputs'], list)
                and all(confirmed(i) and i.get('requires_manual_verification') is not True
                        and i.get('value') is not None for i in evidence['inputs']))
    return True


def sanitize_record(row, path='', metadata=None):
    """Return a fresh row, reset count and rejection reasons; never fill a gap."""
    if not isinstance(row, dict):
        return None, 0, ['invalid_record']
    clean = copy.deepcopy(row)
    resets = 0
    meta = metadata or {}

    def walk(obj, parent_manual=False, inherited_url=None):
        nonlocal resets
        if isinstance(obj, list):
            for child in obj:
                walk(child, parent_manual, inherited_url)
        elif isinstance(obj, dict):
            context = dict(obj)
            if not context.get('source_url') and inherited_url:
                context['source_url'] = inherited_url
            manual = parent_manual or (obj.get('requires_manual_verification') is True and
                      not confirmed(obj.get('manual_verification')))
            for key, value in list(obj.items()):
                if key in {'field_evidence', 'manual_verification'}:
                    continue  # Evidence maps describe claims; they are not measurements.
                sensitive = (key in CAPACITY | METRICS | STOCK | NORMS or
                             key.lower() in CAPACITY | METRICS or key.lower().startswith(('tts_', 'ttr_')))
                if sensitive and isinstance(value, dict) and 'value' in value:
                    # A missing metric plus gap metadata is not an imputed value.
                    if value['value'] is not None and not field_evidence(obj, key, value['value']):
                        value['value'] = None
                        resets += 1
                    walk(value, manual, context.get('source_url'))
                    continue
                unsupported = sensitive and not field_evidence(obj, key, value)
                if key in QUANTITY and manual:
                    unsupported = True
                if unsupported and value is not None:
                    obj[key] = None
                    resets += 1
                else:
                    walk(value, manual, context.get('source_url'))

    walk(clean, inherited_url=meta.get('source_url'))
    reasons = []
    if synthetic_record(row, path, meta):
        reasons.append('synthetic_demo_or_test_origin')
    if row.get('is_eligible_for_calculation') is False or meta.get('is_eligible_for_calculation') is False:
        reasons.append('explicitly_ineligible')
    contexts = list(objects(clean))
    if any(o.get('evidence_class') in NONFACTUAL for o in contexts):
        reasons.append('nonfactual_evidence_class')
    if any(o.get('requires_manual_verification') is True and not confirmed(o.get('manual_verification')) for o in contexts):
        reasons.append('pending_manual_verification')
    context = {**meta, **clean}
    # Wrapped SourceRecord confirmation must live at the wrapper or data claim.
    if context.get('verified') is None and isinstance(clean.get('data'), dict):
        context = {**context, **clean['data']}
    if not confirmed(context):
        reasons.append('missing_explicit_document_confirmation')
    if not timestamp(context.get('collected_at') or context.get('source_fetched_at')):
        reasons.append('missing_collection_timestamp')
    clean['is_eligible_for_calculation'] = not reasons
    return clean, resets, reasons


def discover(root, output):
    """Normalized JSONL/NDJSON and JSON record arrays, not raw response caches."""
    for p in sorted(root.rglob('*')):
        if not p.is_file() or p.suffix not in {'.json', '.jsonl', '.ndjson'}:
            continue
        rel = p.relative_to(root)
        if p.is_relative_to(output) or any(x.startswith('.') or x == 'raw' or 'cache' in x for x in rel.parts):
            continue
        if any(s in p.name for s in ('.responses.', '.inspect.', 'worker_tasks', 'schema.')):
            continue
        yield p


def read_records(path):
    if path.suffix in {'.jsonl', '.ndjson'}:
        with path.open(encoding='utf-8-sig') as stream:
            for line, text in enumerate(stream, 1):
                if text.strip():
                    yield line, json.loads(text)
    else:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if isinstance(value, list):
            for i, row in enumerate(value, 1):
                if isinstance(row, dict) and any(k in row for k in ('record_type', 'source_url', 'foreign_components', 'part_number', 'verified', 'is_synthetic', 'is_demo')):
                    yield i, row
        elif isinstance(value, dict) and isinstance(value.get('records'), list):
            for i, row in enumerate(value['records'], 1):
                yield i, row
        elif isinstance(value, dict) and any(k in value for k in ('is_synthetic', 'is_demo', 'verified')):
            yield 1, value


def audit(root, output):
    root, output = root.resolve(), output.resolve()
    paths = list(discover(root, output))
    output.mkdir(parents=True, exist_ok=True)
    summary = dict(total_records_inspected=0, synthetic_records_removed=0,
                   hardcoded_constants_reset_to_null=0, eligible_records_passed=0)
    reasons_count, files, failures = Counter(), [], []
    seen = set()
    with (output/'sanitization_report.json').open('w', encoding='utf-8') as report, (output/'rejected_records.jsonl').open('w', encoding='utf-8') as rejected:
        report.write('{"cleaned_dataset":[')
        first = True
        for path in paths:
            file_info = dict(path=str(path.relative_to(root)), sha256=None, inspected=0, eligible=0, synthetic=0)
            h = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    h.update(block)
            file_info['sha256'] = h.hexdigest()
            try:
                for line, row in read_records(path):
                    summary['total_records_inspected'] += 1
                    file_info['inspected'] += 1
                    clean, resets, reasons = sanitize_record(row, path)
                    summary['hardcoded_constants_reset_to_null'] += resets
                    synthetic = 'synthetic_demo_or_test_origin' in reasons
                    summary['synthetic_records_removed'] += int(synthetic)
                    file_info['synthetic'] += int(synthetic)
                    fingerprint = json.dumps(clean, sort_keys=True, ensure_ascii=False, allow_nan=False)
                    if not reasons and fingerprint in seen:
                        reasons = ['duplicate_confirmed_record']
                        clean['is_eligible_for_calculation'] = False
                    if reasons:
                        reasons_count.update(reasons)
                        rejected.write(json.dumps(dict(input_file=str(path.relative_to(root)), line=line,
                            is_eligible_for_calculation=False, reasons=reasons,
                            reset_to_null_count=resets), ensure_ascii=False, allow_nan=False)+'\n')
                    else:
                        seen.add(fingerprint)
                        if not first:
                            report.write(',')
                        report.write(json.dumps(dict(clean, sanitization_provenance=dict(input_file=str(path.relative_to(root)), line=line, input_sha256=h.hexdigest())), ensure_ascii=False, allow_nan=False))
                        first = False
                        summary['eligible_records_passed'] += 1
                        file_info['eligible'] += 1
            except (ValueError, UnicodeError) as exc:
                failures.append(dict(path=str(path.relative_to(root)), error=str(exc)))
            files.append(file_info)
        report.write('],"sanitization_summary":'+json.dumps(summary)+'}\n')
    details = dict(scope='Normalized JSONL/NDJSON, JSON record arrays/records envelopes; raw caches, databases, compressed archives, CSV and workbooks are not scanned.',
                   verification='Explicit verified=true plus document/quote, public URL and collection timestamp; no automatic promotion of extracted candidates.',
                   summary=summary, rejection_reasons=dict(reasons_count), files=files, errors=failures,
                   audit_complete=not failures)
    (output/'audit_details.json').write_text(json.dumps(details, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return details


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--output', type=Path, default=Path('outputs/sanitization_20261010'))
    args = parser.parse_args()
    result = audit(args.root, args.output)
    print(json.dumps(result['summary']))
    raise SystemExit(0 if result['audit_complete'] else 1)
