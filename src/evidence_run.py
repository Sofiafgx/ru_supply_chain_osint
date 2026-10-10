"""Resume one ARSHIN batch and follow an existing Comtrade run with live audits."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from evidence_snapshot import snapshot

ROOT = Path(__file__).resolve().parents[1]


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def execute(output, trade_pid, pages):
    import fcntl
    output.mkdir(parents=True, exist_ok=True)
    with (output / 'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Evidence run is already active')
        started = datetime.now(timezone.utc).isoformat()
        old_report = ROOT / 'data/processed/equipment/verified_equipment_arshin.jsonl.report.json'
        if old_report.exists():
            (output / 'arshin_before_resume.json').write_bytes(old_report.read_bytes())
        command = [sys.executable, '-u', str(ROOT / 'src/arshin_parser.py'),
                   '--facility-names', str(ROOT / 'config/facility_names.json'),
                   '--max-pages', str(pages), '--proxy-start-port', '10495']
        with (output / 'arshin.log').open('a') as log:
            child = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            while True:
                active = dict(arshin=child.poll() is None,
                              comtrade=bool(trade_pid and alive(trade_pid)))
                try:
                    coverage = snapshot(ROOT, ROOT / 'outputs/evidence_pipeline')
                    audit_error = None
                except (OSError, ValueError) as exc:
                    coverage = None
                    audit_error = type(exc).__name__
                state = dict(started_at=started, updated_at=datetime.now(timezone.utc).isoformat(),
                             supervisor_pid=os.getpid(), arshin_pid=child.pid,
                             comtrade_pid=trade_pid, active=active,
                             arshin_exit_code=child.poll(), arshin_batch_pages=pages,
                             status='running' if any(active.values()) else 'finished',
                             counts=coverage['counts'] if coverage else None,
                             pending_queries=len(coverage['pending_queries']) if coverage else None,
                             audit_error=audit_error)
                temporary = output / 'run.json.tmp'
                temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
                temporary.replace(output / 'run.json')
                if not any(active.values()):
                    break
                time.sleep(30)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs/evidence_run')
    parser.add_argument('--comtrade-pid', type=int)
    parser.add_argument('--arshin-pages', type=int, default=1)
    parser.add_argument('--detach', action='store_true')
    args = parser.parse_args()
    if args.arshin_pages < 1:
        parser.error('--arshin-pages must be positive')
    if args.detach:
        args.output.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, '-u', str(Path(__file__).resolve()), '--output', str(args.output),
                   '--arshin-pages', str(args.arshin_pages)]
        if args.comtrade_pid:
            command += ['--comtrade-pid', str(args.comtrade_pid)]
        with (args.output / 'supervisor.log').open('a') as log:
            child = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
        print(json.dumps(dict(supervisor_pid=child.pid, status='started')))
    else:
        execute(args.output, args.comtrade_pid, args.arshin_pages)
