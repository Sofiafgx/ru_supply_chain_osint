"""Freeze existing collector outputs and import them without touching live files."""
import argparse
import hashlib
import json
from pathlib import Path
from evidence_pipeline import run


def snapshot(root, output, pilot_file=None):
    output.mkdir(parents=True, exist_ok=True)
    inputs, queries, pending = [], [], []
    pilots = json.loads(pilot_file.read_text()) if pilot_file else []
    if not pilots:
        pending.append(dict(source='contracts_and_court', reason='approved_20_pilot_scope_missing'))
    for name, kind in [('data/processed/trade/monthly_trade_mirror.jsonl', 'trade'), ('data/processed/equipment/verified_equipment_arshin.jsonl', 'equipment'),
                       ('outputs/tier2_audit/contracts.ndjson', 'contract'),
                       ('outputs/tier2_audit/court_cases.ndjson', 'court')]:
        source = root / name
        if source.exists():
            target = output / Path(name).name
            # Collectors publish trade snapshots via atomic replacement. ARSHIN
            # appends: ignore a last incomplete line, retaining a visible audit gap.
            raw = source.read_bytes()
            if raw and not raw.endswith(b'\n'):
                raw = raw[:raw.rfind(b'\n') + 1]
                pending.append(dict(source=kind, reason='incomplete_final_jsonl_line'))
            target.write_bytes(raw)
            inputs.append(dict(path=target.name, kind=kind))
        else:
            pending.append(dict(source=kind, reason='collector_output_missing'))
    trade = root / 'data/processed/trade/monthly_trade_mirror.coverage.json'
    if trade.exists():
        report = json.loads(trade.read_text())
        for q in report.get('coverage', []):
            item = dict(query_id=f"trade:{q['period']}:{q['reporter_code']}:{q['hs_code']}", source='comtrade', details=q)
            status = q.get('status')
            if status in ('ok', 'no_data'):
                item['category'] = 'found' if status == 'ok' else 'no_result'
                queries.append(item)
            elif status in ('error', 'deferred'):
                item['category'] = 'technical_access_unavailable'
                queries.append(item)
            else:
                pending.append(item)
    arshin = root / 'data/processed/equipment/verified_equipment_arshin.jsonl.report.json'
    if arshin.exists():
        report = json.loads(arshin.read_text())
        item = dict(query_id='arshin:configured_registry_scan', source='arshin', details=report)
        if report.get('complete'):
            item['category'] = 'found' if report.get('saved_records', 0) else 'no_result'
            queries.append(item)
        elif report.get('status') in ('failed', 'error'):
            item['category'] = 'technical_access_unavailable'
            queries.append(item)
        else:
            pending.append(item)
    config = dict(pilot_inns=pilots, inputs=inputs, queries=queries, pending_queries=pending)
    manifest = output / 'manifest.json'
    manifest.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
    return run(manifest, output / 'evidence.sqlite3', output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, default=Path('outputs/evidence_pipeline'))
    parser.add_argument('--pilot-file', type=Path, help='Approved JSON array of exactly 20 INNs')
    args = parser.parse_args()
    result = snapshot(args.root, args.output, args.pilot_file)
    print(json.dumps(dict(counts=result['counts'], pending_queries=len(result['pending_queries']))))
