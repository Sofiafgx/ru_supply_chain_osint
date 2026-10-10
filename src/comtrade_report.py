"""Publish only monthly trade volumes and observed rhythms from Comtrade."""
import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from comtrade_mirror_async import add_intervals, PRIORITY_HS_CODES

ROOT = Path(__file__).resolve().parents[1]


def publish(output):
    output.mkdir(parents=True, exist_ok=True)
    coverage = json.loads((ROOT/'data/processed/trade/monthly_trade_mirror.coverage.json').read_text())
    selected = set(coverage.get('hs_codes', PRIORITY_HS_CODES))
    rows = [json.loads(line) for line in (ROOT/'data/processed/trade/monthly_trade_mirror.jsonl').read_text().splitlines() if line.strip()]
    rows = [r for r in rows if r['hs_code'] in selected]
    add_intervals(rows, coverage['threshold_usd'])
    fields = ['period','reporter_code','reporter_name','partner_code','hs_code','flow_code',
              'trade_value_usd','net_weight_kg','quantity','quantity_unit',
              'large_monthly_total','previous_large_period','delta_t_months','delta_t_days','interval_basis']
    temporary = output/'monthly_trade.csv.tmp'
    with temporary.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
        writer.writeheader();writer.writerows(rows)
    temporary.replace(output/'monthly_trade.csv')
    counts = dict(Counter(q['status'] for q in coverage.get('coverage', [])))
    report = dict(generated_at=datetime.now(timezone.utc).isoformat(), scope='monthly_trade_only',
                  hs_codes=sorted(selected), partner_code=643, records=len(rows),
                  collector_status=coverage['status'], query_status_counts=counts,
                  series_complete=coverage['queries_complete'],
                  peak_threshold_usd=coverage['threshold_usd'],
                  interval_basis='Intervals between observed monthly totals above threshold; incomplete query coverage may conceal peaks.',
                  source='UN Comtrade monthly mirror exports; X and RX are separate flows')
    (output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    lines = ['# Обсяги й ритмічність торгівлі', '',
             f"Оновлено: {report['generated_at']}.",
             f"Напрям: звітований експорт країн-партнерів до РФ. HS: {', '.join(sorted(selected))}.",
             f"Наявних місячних записів: {len(rows)}. Статус збору: {coverage['status']}.",
             f"Статуси запитів: {counts}.", '',
             '| HS | Наявні записи X | Сума наявних значень X, USD |',
             '|---|---:|---:|']
    for hs in sorted(selected):
        group = [r for r in rows if r['hs_code']==hs and r['flow_code']=='X']
        values = [r['trade_value_usd'] for r in group if r.get('trade_value_usd') is not None]
        amount = f'{sum(values):,.2f}' if values else 'немає даних'
        lines.append(f'| {hs} | {len(group)} | {amount} |')
    lines += ['', 'Суми охоплюють лише наявні відповіді, а не весь запитаний період.',
              'Файл monthly_trade.csv містить помісячні обсяги, вагу, кількість та інтервали між наявними місячними піками для кожної країни, HS і потоку.',
              f"Порогове значення піка: понад {coverage['threshold_usd']:,.0f} USD за місяць.",
              'X та RX подано окремо й не додаються разом. Порожня відповідь не замінюється нульовим обсягом.',
              'За неповного ряду інтервали описують тільки наявні піки; між ними можуть бути пропущені дані.']
    (output/'trade_report_uk.md').write_text('\n'.join(lines)+'\n')
    return report


def watch(output, pid):
    while True:
        report = publish(output)
        if not pid:
            return report
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return publish(output)
        time.sleep(30)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'outputs/comtrade_only')
    parser.add_argument('--watch-pid',type=int)
    parser.add_argument('--detach',action='store_true')
    args=parser.parse_args()
    if args.detach:
        args.output.mkdir(parents=True,exist_ok=True)
        command=[sys.executable,'-u',str(Path(__file__).resolve()),'--output',str(args.output)]
        if args.watch_pid:command+=['--watch-pid',str(args.watch_pid)]
        with (args.output/'report.log').open('a') as log:
            child=subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        print(json.dumps(dict(report_pid=child.pid,status='started')))
    else:
        result=watch(args.output,args.watch_pid)
        print(json.dumps(dict(records=result['records'],status=result['collector_status'])))
