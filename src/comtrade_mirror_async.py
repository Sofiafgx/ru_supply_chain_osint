"""Collect monthly UN Comtrade mirror exports to Russia; resumable public preview."""
from __future__ import annotations

import argparse
import hashlib
import asyncio
import json
import logging
import math
import random
import sqlite3
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

import httpx

API_URL = 'https://comtradeapi.un.org/public/v1/preview/C/M/HS'
HS_CODES = ('854231', '854232', '854233', '854239', '852691', '845710', '845811', '845961')
PRIORITY_HS_CODES = HS_CODES[:5]
REPORTERS = (156, 792, 784, 398, 51, 688)
FLOWS = ('X', 'RX')
MAX_RECORDS = 500
LOG = logging.getLogger('comtrade')


class CappedRows(list):
    pass


class CollectionError(Exception):
    pass


class BudgetExceeded(CollectionError):
    pass


class Deferred(CollectionError):
    pass


def month_number(period: str) -> int:
    parsed = datetime.strptime(period, '%Y-%m')
    if parsed.strftime('%Y-%m') != period:
        raise ValueError('Expected YYYY-MM')
    return parsed.year * 12 + parsed.month - 1


def months(start: str, end: str) -> list[str]:
    first, last = month_number(start), month_number(end)
    if first > last:
        raise ValueError('Start month must precede end month')
    return [f'{n // 12:04d}-{n % 12 + 1:02d}' for n in range(first, last + 1)]


def query_params(period: str, reporter: int, hs: str, flow: str = 'X,RX') -> dict:
    return dict(period=period.replace('-', ''), reporterCode=reporter,
                partnerCode=643, partner2Code=0, cmdCode=hs, flowCode=flow,
                customsCode='C00', motCode=0, maxRecords=MAX_RECORDS,
                breakdownMode='classic', includeDesc='true', format='JSON')


def query_key(params: dict) -> str:
    return json.dumps(params, sort_keys=True, separators=(',', ':'))


class State:
    """Successful responses and a persistent UTC-day request budget (retries included)."""
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS responses
              (key TEXT PRIMARY KEY, payload TEXT NOT NULL, fetched_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS usage
              (day TEXT PRIMARY KEY, calls INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS settings
              (key TEXT PRIMARY KEY, value REAL NOT NULL);
        ''')

    def get(self, params: dict):
        row = self.db.execute('SELECT payload FROM responses WHERE key=?',
                              (query_key(params),)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, params: dict, rows: list):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO responses VALUES (?, ?, ?)',
                            (query_key(params), json.dumps(rows, allow_nan=False),
                             datetime.now(timezone.utc).isoformat()))

    def reserve(self, daily_budget: int):
        day = datetime.now(timezone.utc).date().isoformat()
        # Reserve before sending: even failed HTTP calls consume the local budget.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT calls FROM usage WHERE day=?', (day,)).fetchone()
            if row and row[0] >= daily_budget:
                raise BudgetExceeded(f'Local daily budget {daily_budget} exhausted; resume tomorrow')
            self.db.execute('INSERT INTO usage VALUES (?, 1) ON CONFLICT(day) '
                            'DO UPDATE SET calls=calls+1', (day,))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def cooldown(self) -> float:
        row = self.db.execute("SELECT value FROM settings WHERE key='not_before'").fetchone()
        return row[0] if row else 0.0

    def defer(self, seconds: float):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('not_before', ?)",
                            (max(self.cooldown(), time.time() + seconds),))


def retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


class Collector:
    def __init__(self, client: httpx.AsyncClient, state: State, *, rps=1.0,
                 daily_budget=500, retries=5, refresh=False, archive_path=None):
        self.client, self.state = client, state
        self.archive_path = archive_path
        self.spacing, self.daily_budget = max(4.0, 1.0 / rps), daily_budget
        self.retries, self.refresh = retries, refresh
        self.lock = asyncio.Lock()
        self.next_start = 0.0
        self.requests = 0
        self.halted: str | None = None

    async def gate(self):
        async with self.lock:
            while True:
                if self.halted:
                    raise Deferred(self.halted)
                cooldown = self.state.cooldown() - time.time()
                if cooldown > 120.5:
                    self.halted = f'API cooldown until {datetime.fromtimestamp(self.state.cooldown(), timezone.utc).isoformat()}'
                    raise Deferred(self.halted)
                delay = max(0.0, cooldown, self.next_start - time.monotonic())
                if delay <= 0:
                    break
                # Another in-flight response can extend cooldown during this wait.
                await asyncio.sleep(min(delay, 30))
            try:
                self.state.reserve(self.daily_budget)
            except BudgetExceeded as exc:
                self.halted = str(exc)
                raise
            self.next_start = time.monotonic() + self.spacing
            self.requests += 1

    async def request(self, params: dict) -> list[dict]:
        for attempt in range(self.retries + 1):
            await self.gate()
            delay = None
            try:
                response = await self.client.get(API_URL, params=params)
                if self.archive_path:
                    from collector_archive import archive_response
                    archive_response(self.archive_path, response)
                if response.status_code == 429 or response.status_code in (500, 502, 503, 504):
                    delay = retry_after(response.headers.get('Retry-After'))
                    problem = f'HTTP {response.status_code}'
                    if response.status_code == 429 and delay is None:
                        delay = min(120.0, 60.0 * (2 ** attempt))
                else:
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise CollectionError('API response is not an object')
                    error = payload.get('error') or payload.get('errorMessage')
                    if error:
                        if 'rate' in str(error).lower() and 'limit' in str(error).lower():
                            problem = str(error)
                        else:
                            raise CollectionError(f'API error: {error}')
                    else:
                        rows = payload.get('data')
                        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                            raise CollectionError('API data must be an array of objects')
                        count = payload.get('count')
                        if count is not None and int(count) > len(rows):
                            if int(count) < MAX_RECORDS:
                                raise CollectionError(f'Truncated response: count={count}, rows={len(rows)}')
                            return CappedRows(rows)
                        return rows
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                problem = type(exc).__name__
            if delay is None:
                delay = min(45.0, 2 ** attempt) + random.uniform(0, 0.5)
            # Shared cooldown applies to ALL workers and survives a restart.
            self.state.defer(delay)
            if attempt == self.retries:
                if problem == 'HTTP 429':
                    self.halted = f'{problem}; retries exhausted; cache preserved'
                    raise Deferred(self.halted)
                raise CollectionError(f'{problem}; retries exhausted')
            LOG.warning('%s; retry after %.1fs', problem, delay)
        raise AssertionError('Unreachable')

    async def fetch(self, params: dict) -> list[dict]:
        cached = None if self.refresh else self.state.get(params)
        if cached is not None:
            return cached
        rows = await self.request(params)
        # Preview has no documented offset/cursor pagination. Split response flows
        # if the cap is hit; never invent an offset which the API may ignore.
        if isinstance(rows, CappedRows) or len(rows) >= MAX_RECORDS:
            flows = params['flowCode'].split(',')
            if len(flows) == 1:
                raise CollectionError('Preview cap reached for an indivisible query; '
                                      'use official authenticated download/bulk API')
            rows = []
            for flow in flows:
                rows.extend(await self.fetch({**params, 'flowCode': flow}))
        # Validate before caching; malformed rows must not poison resumed runs.
        normalize(rows, params)
        self.state.put(params, rows)
        return rows


def number(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise CollectionError('Boolean numeric value')
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise CollectionError(f'Invalid numeric value: {value}')
    return result


def normalize(rows: list[dict], params: dict) -> list[dict]:
    result, seen = [], {}
    for raw in rows:
        for field in ('period', 'reporterCode', 'partnerCode', 'partner2Code', 'cmdCode', 'customsCode', 'motCode'):
            if str(raw.get(field)) != str(params[field]):
                raise CollectionError(f'Unexpected {field}: {raw.get(field)}')
        if raw.get('freqCode') != 'M' or raw.get('flowCode') not in params['flowCode'].split(','):
            raise CollectionError('Unexpected frequency or trade flow')
        period = str(raw['period'])
        row = dict(period=f'{period[:4]}-{period[4:]}',
                   reporter_code=int(raw['reporterCode']), reporter_name=raw.get('reporterDesc'),
                   hs_code=str(raw['cmdCode']), flow_code=raw['flowCode'],
                   net_weight_kg=number(raw.get('netWgt')),
                   trade_value_usd=number(raw.get('primaryValue') if raw.get('primaryValue') is not None
                                          else raw.get('fobvalue')),
                   quantity=number(raw.get('qty')),
                   quantity_unit_code=raw.get('qtyUnitCode'), quantity_unit=raw.get('qtyUnitAbbr'),
                   classification_code=raw.get('classificationCode'),
                   is_reported=raw.get('isReported'),
                   is_quantity_estimated=raw.get('isQtyEstimated'),
                   is_net_weight_estimated=raw.get('isNetWgtEstimated'),
                   source_endpoint=API_URL, partner_code=643, source_query=params)
        key = (row['period'], row['reporter_code'], row['hs_code'], row['flow_code'])
        if key in seen:
            if seen[key] != row:
                raise CollectionError(f'Conflicting monthly totals for {key}; refusing to double count')
            continue
        seen[key] = row
        result.append(row)
    return result


def add_intervals(rows: list[dict], threshold: float):
    groups = defaultdict(list)
    for row in rows:
        row.update(large_monthly_total=(row['trade_value_usd'] is not None and row['trade_value_usd'] > threshold),
                   previous_large_period=None, delta_t_months=None, delta_t_days=None,
                   interval_basis='observed_monthly_totals_not_shipments')
        groups[(row['reporter_code'], row['hs_code'], row['flow_code'])].append(row)
    for group in groups.values():
        previous = None
        for row in sorted(group, key=lambda r: r['period']):
            if not row['large_monthly_total']:
                continue
            if previous is not None:
                row['previous_large_period'] = previous
                row['delta_t_months'] = month_number(row['period']) - month_number(previous)
                row['delta_t_days'] = (date.fromisoformat(row['period'] + '-01') -
                                       date.fromisoformat(previous + '-01')).days
            previous = row['period']


def heading_intervals(rows, coverage, periods, reporters, codes, threshold=100000):
    """Aggregate total exports only; missing/unknown cells cannot become zero."""
    statuses = {(q['period'], q['reporter_code'], q['hs_code']): q for q in coverage}
    result = {}
    for heading in ('8542','8526'):
        selected = [h for h in codes if h.startswith(heading)]
        monthly = []
        for period in periods:
            cells = [statuses.get((period,r,h),{}) for r in reporters for h in selected]
            values = [r.get('trade_value_usd') for r in rows if r['period']==period and r['reporter_code'] in reporters and r['hs_code'] in selected and r['flow_code']=='X']
            required = {(r,h) for r in reporters for h in selected}
            reported = {(r['reporter_code'],r['hs_code']) for r in rows if r['period']==period and r['reporter_code'] in reporters and r['hs_code'] in selected and r['flow_code']=='X' and r.get('trade_value_usd') is not None}
            complete = bool(selected) and all(c.get('status') == 'ok' for c in cells) and reported == required and period < datetime.now(ZoneInfo('Europe/Kyiv')).strftime('%Y-%m')
            total = sum(values) if complete else None
            monthly.append(dict(period=period,complete=complete,value_usd=total,unreported_export_cells=len(required-reported),observed_value_usd=sum(v for v in values if v is not None) if any(v is not None for v in values) else None))
        peaks = [m['period'] for m in monthly if m['complete'] and m['value_usd']>threshold]
        observed_peaks = [m['period'] for m in monthly if m['observed_value_usd'] is not None and m['observed_value_usd']>threshold]
        gaps = [(date.fromisoformat(b+'-01')-date.fromisoformat(a+'-01')).days for a,b in zip(peaks,peaks[1:])]
        observed_gaps = [(date.fromisoformat(b+'-01')-date.fromisoformat(a+'-01')).days for a,b in zip(observed_peaks,observed_peaks[1:])]
        complete = bool(monthly) and all(m['complete'] for m in monthly)
        result[heading] = dict(selected_hs6=selected,flow='X',threshold_usd=threshold,series_complete=complete,
            delta_t_interval_days=sum(gaps)/len(gaps) if complete and gaps else None,
            observed_partial_series_interval_days=sum(observed_gaps)/len(observed_gaps) if observed_gaps else None,observed_peak_months=observed_peaks,
            peak_months=peaks,monthly=monthly,
            interpretation='External import channel rhythm from monthly mirror exports; not plant inventories or individual shipment dates; selected HS6 subset, not all goods in the heading. Empty API results are unknown, not zero; the current month is provisional.',
            missing_months=[m['period'] for m in monthly if not m['complete']])
    return result


def atomic_json(path: Path, data, *, jsonl=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w', encoding='utf-8') as handle:
        if jsonl:
            for row in data:
                handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
        else:
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write('\n')
    temp.replace(path)


async def collect(args) -> int:
    requested = months(args.start, args.end)
    current = datetime.now(ZoneInfo('Europe/Kyiv')).strftime('%Y-%m')
    eligible = [p for p in requested if p <= current]
    jobs = [(p, reporter, hs) for p in eligible for reporter in args.reporters for hs in args.hs_codes]
    state = State(args.state)
    rows, coverage = [], []
    imported = {}
    if args.output.exists():
        with args.output.open(encoding='utf-8') as source:
            for line in source:
                if not line.strip():
                    continue
                row = json.loads(line)
                month_number(row['period'])
                if row['reporter_code'] not in REPORTERS or row['hs_code'] not in HS_CODES or row['flow_code'] not in FLOWS or row.get('partner_code',643) != 643:
                    raise CollectionError('Existing JSONL contains unexpected dimensions')
                number(row.get('trade_value_usd'))
                key = (row['period'],row['reporter_code'],row['hs_code'],row['flow_code'])
                if key in imported and imported[key] != row:
                    raise CollectionError('Conflicting records in existing JSONL')
                imported[key] = row
    rows.extend(imported.values())
    row_index = dict(imported)
    imported_complete = set()
    old_coverage = args.output.with_suffix('.coverage.json')
    if old_coverage.exists() and not args.refresh:
        old_report = json.loads(old_coverage.read_text())
        if old_report.get('endpoint') == API_URL and old_report.get('partner_code') == 643:
            for q in old_report.get('coverage',[]):
                key = (q['period'],q['reporter_code'],q['hs_code'])
                saved = [r for k,r in imported.items() if k[:3] == key]
                if q.get('status') in ('ok','no_data') and q.get('records') == len(saved) and sorted(q.get('returned_flows',[])) == sorted(r['flow_code'] for r in saved):
                    imported_complete.add(key)
    def checkpoint(status):
        snapshot = list(row_index.values())
        add_intervals(snapshot, args.threshold)
        snapshot.sort(key=lambda r:(r['period'],r['reporter_code'],r['hs_code'],r['flow_code']))
        completed = {(q['period'],q['reporter_code'],q['hs_code']): q for q in coverage}
        full_coverage = [completed.get(job, dict(period=job[0],reporter_code=job[1],hs_code=job[2],status='pending')) for job in jobs]
        complete = all(q['status'] in ('ok','no_data') for q in full_coverage)
        report = dict(endpoint=API_URL,generated_at=datetime.now(timezone.utc).isoformat(),status=status,
                      start=args.start,end=args.end,partner_code=643,reporters=args.reporters,hs_codes=args.hs_codes,
                      future_months_skipped=[p for p in requested if p>current],queries_complete=complete,
                      records=len(snapshot),http_requests_this_run=collector.requests,
                      threshold_usd=args.threshold,cache_refresh=args.refresh,imported_jsonl_records=len(imported),
                      interval_basis='Monthly export totals X; RX is reported separately and never added to X. First day of month is a date proxy; incomplete coverage can conceal peaks.',
                      preview_cap_strategy='Queries are already partitioned by month, reporter, HS6. At the cap split X/RX; indivisible capped HS6 requires bulk/authenticated API. No invented national subpositions.',
                      current_month_provisional=current if current in eligible else None,coverage=full_coverage)
        atomic_json(args.output,snapshot,jsonl=True)
        atomic_json(args.output.with_suffix('.coverage.json'),report)
        atomic_json(args.output.with_suffix('.intervals.json'),heading_intervals(snapshot,full_coverage,eligible,args.reporters,args.hs_codes,args.threshold))
        return report

    queue = asyncio.Queue()
    for job in jobs:
        queue.put_nowait(job)
    try:
        async with httpx.AsyncClient(timeout=args.timeout,
                                     limits=httpx.Limits(max_connections=args.workers),
                                     headers={'User-Agent': 'MonthlyTradeMirror/1.0'}) as client:
            collector = Collector(client, state, rps=args.rps, daily_budget=args.daily_budget,
                                  retries=args.retries, refresh=args.refresh,
                                  archive_path=args.output.with_name(args.output.name + ".responses.jsonl"))

            checkpoint('running')

            async def worker():
                while not queue.empty():
                    period, reporter, hs = queue.get_nowait()
                    item = dict(period=period, reporter_code=reporter, hs_code=hs)
                    try:
                        params = query_params(period, reporter, hs)
                        if (period,reporter,hs) in imported_complete:
                            data = [r for k,r in imported.items() if k[:3] == (period,reporter,hs)]
                        else:
                            data = normalize(await collector.fetch(params), params)
                        for key in list(row_index):
                            if key[:3] == (period,reporter,hs):
                                del row_index[key]
                        fetched = state.db.execute('SELECT fetched_at, payload FROM responses WHERE key=?', (query_key(params),)).fetchone()
                        cache_path = None
                        if fetched:
                            cache_dir = args.output.with_name(args.output.name + '.evidence')
                            cache_dir.mkdir(exist_ok=True)
                            digest = hashlib.sha256(fetched[1].encode()).hexdigest()
                            cache_path = cache_dir / (digest + '.json')
                            cache_path.write_text(fetched[1], encoding='utf-8')
                        for r in data:
                            r.setdefault('source_endpoint',API_URL)
                            r.setdefault('partner_code',643)
                            r.setdefault('source_query',params)
                            r.setdefault('source_fetched_at', fetched[0] if fetched else None)
                            r.setdefault('collected_at', r.get('source_fetched_at'))
                            r.setdefault('source_url', API_URL + '?' + urlencode(params))
                            if cache_path and (period, reporter, hs) not in imported_complete:
                                r['input_sha256'] = digest
                                r['input_file'] = str(cache_path.resolve())
                            row_index[(r['period'],r['reporter_code'],r['hs_code'],r['flow_code'])] = r
                        item.update(status='ok' if data else 'no_data',
                                    returned_flows=sorted({r['flow_code'] for r in data}), records=len(data))
                    except asyncio.CancelledError:
                        item.update(status='deferred',error='Collection interrupted; cached records preserved')
                        raise
                    except (CollectionError, httpx.HTTPError, ValueError, TypeError) as exc:
                        item.update(status='deferred' if isinstance(exc, (BudgetExceeded, Deferred)) else 'error',
                                    error=str(exc))
                        if not collector.halted:
                            LOG.error('%s: %s', item, exc)
                    finally:
                        coverage.append(item)
                        queue.task_done()
                        checkpoint('running')
                    if len(coverage) % 100 == 0:
                        LOG.info('Processed %d/%d queries; %d records', len(coverage), len(jobs), len(row_index))

            await asyncio.gather(*(worker() for _ in range(args.workers)))
        report = checkpoint('completed' if all(q['status'] in ('ok','no_data') for q in coverage) else 'partial')
        report['http_requests_this_run'] = collector.requests
        atomic_json(args.output.with_suffix('.coverage.json'),report)
        LOG.info('Saved %d records to %s; queries_complete=%s', report['records'], args.output, report['queries_complete'])
        return 0 if report['queries_complete'] else 2
    except asyncio.CancelledError:
        checkpoint('interrupted')
        raise
    finally:
        state.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', default='2024-01', help='YYYY-MM')
    parser.add_argument('--end', default='2026-12', help='YYYY-MM; future months are skipped')
    parser.add_argument('--output', type=Path, default=Path('data/processed/trade/monthly_trade_mirror.jsonl'))
    parser.add_argument('--state', type=Path, default=Path('state/.comtrade_mirror.sqlite3'))
    parser.add_argument('--reporters', nargs='+', type=int, choices=REPORTERS, default=list(REPORTERS))
    parser.add_argument('--hs-codes', nargs='+', choices=HS_CODES, default=list(PRIORITY_HS_CODES))
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--rps', type=float, default=0.25, help='Global request rate, capped at 0.25 (4 second spacing)')
    parser.add_argument('--daily-budget', type=int, default=500, help='Local conservative daily cap including retries')
    parser.add_argument('--retries', type=int, default=5)
    parser.add_argument('--timeout', type=float, default=45)
    parser.add_argument('--threshold', type=float, default=100000)
    parser.add_argument('--refresh', action='store_true', help='Refresh cached responses, including empty results')
    args = parser.parse_args()
    try:
        months(args.start, args.end)
    except ValueError as exc:
        parser.error(str(exc))
    if (not 0 < args.rps <= 0.25 or not math.isfinite(args.timeout) or args.timeout <= 0
            or args.workers < 1 or args.daily_budget < 1 or args.retries < 0
            or not math.isfinite(args.threshold) or args.threshold < 0):
        parser.error('Require 0 < rps <= 0.25, positive timeout/workers/budget, nonnegative retries/threshold')
    args.reporters = sorted(set(args.reporters))
    args.hs_codes = sorted(set(args.hs_codes))
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    # Prevent simultaneous runs sharing state/output from violating rate limits.
    import fcntl
    args.state.parent.mkdir(parents=True, exist_ok=True)
    with args.state.with_suffix(args.state.suffix + '.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('Another collector is using this state file')
        raise SystemExit(asyncio.run(collect(args)))


if __name__ == '__main__':
    main()
