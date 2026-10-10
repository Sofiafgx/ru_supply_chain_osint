"""Run collectors and publish a live combined evidence/coverage report."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]

def read(path):
    try: return json.loads(path.read_text())
    except (OSError, ValueError): return None

def write(path,data):
    temp=path.with_name(path.name+'.tmp')
    temp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    temp.replace(path)

def publish(processes,started):
    contracts=read(ROOT/'outputs/tier2_audit/coverage.json')
    arshin=read(ROOT/'data/processed/equipment/verified_equipment_arshin.jsonl.report.json')
    trade=read(ROOT/'data/processed/trade/monthly_trade_mirror.coverage.json')
    metrics=read(ROOT/'data/processed/trade/monthly_trade_mirror.intervals.json')
    status={name:dict(pid=p.pid,status='running' if p.poll() is None else 'finished',exit_code=p.poll()) for name,p in processes.items()}
    report=dict(generated_at=datetime.now(timezone.utc).isoformat(),started_at=started,
        status='running' if any(p.poll() is None for p in processes.values()) else 'finished_with_gaps',processes=status,
        contracts=contracts,arshin=arshin,comtrade=trade,interval_metrics=metrics,
        data_provenance_policy='No anonymous supplier edges, no guessed identifiers, missing facts null. Country-level trade does not establish firm-level supply edges.',
        remaining_gaps=['Supplier INNs missing in all 13 target-customer contracts','Four supplied target INNs fail checksum; not substituted','No plant/distributor pairs were identified for KAD checks','Arshin full scan is complete only if its report complete=true; offset pagination over a live index is not a frozen snapshot','Trade lag remains null until selected monthly series coverage is complete','Empty API responses are not confirmed zero trade; unpublished/current/future months remain data gaps'])
    if trade:
        report['comtrade_status_counts']=dict(Counter(q.get('status','unknown') for q in trade.get('coverage',[])))
    write(ROOT/'reports/status/coverage.json',report)
    lines=['# Оновлення джерел даних',f'Стан на {report["generated_at"]}.',
        '',f'Контракти: {contracts["source_records"] if contracts else "невідомо"} перевірених записів; підтверджених пар завод–дистриб’ютор: {contracts["unique_pairs"] if contracts else "невідомо"}; сума підтверджених угод: {contracts["confirmed_contract_total_rub"] if contracts else "невідомо"} RUB.',
        'Усі 13 контрактів, де замовник — заданий завод, мають порожні реквізити постачальника. Список пар для KAD порожній; перевірку спорів не можна вважати виконаною.',
        '',f'Аршин: статус {arshin.get("status") if arshin else "starting"}; сторінок у поточному запуску {arshin.get("pages") if arshin else 0}; карток {arshin.get("cards") if arshin else 0}; нових збігів {arshin.get("matched") if arshin else 0}; повнота {arshin.get("complete") if arshin else False}.',
        '',f'Comtrade: рядків {trade.get("records") if trade else 0}; повнота запитів {trade.get("queries_complete") if trade else False}; статуси {report.get("comtrade_status_counts",{})}.']
    for h in ('8542','8526'):
        m=(metrics or {}).get(h,{})
        lines.append(f'Δt ({h}, лише вибрані HS6, експорт X): {m.get("delta_t_interval_days")} днів; повнота ряду {m.get("series_complete",False)}.')
    lines.extend(['','Δt характеризує ритмічність зовнішнього каналу імпорту. Місячні агрегати не є датами окремих поставок і не визначають внутрішньозаводський запас S_safe. Реекспорт RX не додається до X, щоб уникнути подвійного підрахунку.',
        'Задані 854231/854232/854233/854239/852691 — вже HS6. Неподільний запит, обрізаний Preview, потребує офіційного Bulk/API; вигадані національні субпозиції не використовуються.',
        'Офіційні пояснення: https://uncomtrade.org/docs/what-is-data-preview/ ; https://uncomtrade.org/docs/distinction-between-exports-and-re-exports-imports-and-re-imports/',
        'Кеш початкових 18 рядків збережено; записи поза п’ятьма пріоритетними кодами зберігаються у файлі, але виключені з поточних лагових метрик.'])
    (ROOT/'outputs/data_update_report_uk.md').write_text('\n\n'.join(lines)+'\n')

def run():
    import fcntl
    with (ROOT/'state/.data_update.lock').open('a') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise SystemExit('Data update already running')
        started=datetime.now(timezone.utc).isoformat()
        commands={
            'arshin':[sys.executable,'-u','src/arshin_parser.py','--facility-names','config/facility_names.json','--proxy-start-port','10495'],
            'comtrade':[sys.executable,'-u','src/comtrade_mirror_async.py','--workers','1','--rps','0.25','--daily-budget','1500']}
        processes={};logs=[]
        for name,command in commands.items():
            log=(ROOT/'outputs'/f'{name}_update.log').open('a')
            logs.append(log)
            log.write(f'\nRun {started}\n');log.flush()
            processes[name]=subprocess.Popen(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
        write(ROOT/'outputs/data_update_run.json',dict(supervisor_pid=os.getpid(),started_at=started,collector_sha256={name:hashlib.sha256((ROOT/command[2]).read_bytes()).hexdigest() for name,command in commands.items()},processes={n:dict(pid=p.pid,command=commands[n],log=str(ROOT/'outputs'/f'{n}_update.log')) for n,p in processes.items()}))
        while True:
            publish(processes,started)
            if all(p.poll() is not None for p in processes.values()): break
            time.sleep(10)
        for log in logs: log.close()
        from evidence_snapshot import snapshot
        snapshot(ROOT, ROOT/'outputs/evidence_pipeline')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--detach',action='store_true');a=p.parse_args()
    if a.detach:
        log=(ROOT/'outputs/data_update_supervisor.log').open('a')
        child=subprocess.Popen([sys.executable,'-u',str(Path(__file__).resolve())],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        print(json.dumps(dict(supervisor_pid=child.pid,status='started')))
    else:run()
