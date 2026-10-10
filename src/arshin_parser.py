"""Collect public ARSHIN verification cards through an HTTP proxy pool."""
from __future__ import annotations

import argparse
from collections import OrderedDict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import sqlite3
import hashlib
import time
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx

DEFAULT_INNS = ['1646019864', '1804002498', '2130094170', '5018077598', '6612001460', '7719830028', '6444009038', '7726019325', '7722488005', '5243001887']
BASE = 'https://fgis.gost.ru/fundmetrology/cm'
SEARCH_URL = BASE + '/xcdb/vri/select'
FALLBACK_URL = BASE + '/xcdb/vri/select'
DETAIL_URL = BASE + '/iaux/vri/{vri_id}'
TRANSIENT_STATUS_CODES = {429, 499, 502, 503, 504}
MARKERS = ('координатно', 'КИМ', 'интерферометр', 'ЧПУ', 'станок', 'станки',
           'балансировоч', 'Zeiss', 'Renishaw', 'Mitutoyo', 'Heidenhain',
           'Marposs', 'DMG', 'Haas')
EQUIPMENT = re.compile(
    r'координатно[\s-]*измеритель|\bКИМ\b|интерферометр|\bЧПУ\b|'
    r'\bстан(?:ок|ки|ков|ка|ке|ку)\b|балансировоч\w*\s+стенд|'
    r'\b(?:Zeiss|Renishaw|Mitutoyo|Heidenhain|Marposs|DMG[\s-]*Mori|Haas)\b', re.I)
MISSING = {'', '-', '—', 'нет данных', 'нет сведений', 'не указан', 'не указано',
           'отсутствует', 'null', 'none', 'б/н', 'без номера'}


class ProtocolError(RuntimeError):
    pass


def clean(value):
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).replace('\ufffc', '').strip()
    return None if text.casefold() in MISSING else text


def normalized_name(value):
    name = re.sub(r'[^\w]+', ' ', str(value).casefold().replace('ё', 'е')).strip()
    # User aliases omit legal-form prefixes; retain the substantive company name.
    return re.sub(r'^(?:(?:пао|оао|зао|ао|ооо)\s+|акционерное общество\s+)', '', name).strip()


def load_proxies(path):
    """Accept URLs, host:port and host:port:user:password; retain ports 10000..10999."""
    proxies = []
    for number, line in enumerate(Path(path).read_text(encoding='utf-8').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if '://' not in line:
            parts = line.split(':')
            if len(parts) == 4:
                host, port, user, password = parts
                line = f'{quote(user, safe="")}:{quote(password, safe="")}@{host}:{port}'
            line = 'http://' + line
        try:
            parsed = urlsplit(line)
            if parsed.scheme != 'http' or not parsed.hostname or parsed.port is None:
                raise ValueError()
            if 10000 <= parsed.port <= 10999 and line not in proxies:
                proxies.append(line)
        except ValueError:
            raise ValueError(f'Некоректний HTTP-проксі: рядок {number} (адресу приховано)') from None
    if not proxies:
        raise ValueError('У файлі немає HTTP-проксі з портами 10000..10999')
    return sorted(proxies, key=lambda address: urlsplit(address).port)


class ProxySession:
    """Round-robin requests; reuse bounded per-proxy httpx sessions and cookies."""
    def __init__(self, proxies, timeout=15, retries=5, delay=0.5, client_factory=httpx.Client,
                 read_timeout=50, base_delay=3, archive_path=None):
        if not proxies:
            raise ValueError('Пул проксі порожній')
        self.proxies = proxies
        self.timeout = httpx.Timeout(connect=timeout, read=read_timeout, write=timeout, pool=30)
        self.retries, self.delay, self.base_delay = retries, delay, base_delay
        self.client_factory = client_factory
        self.clients = OrderedDict()
        self.cursor = 0
        self.last_request = 0.0
        self.request_count = 0
        self.disabled = {}
        self.on_event = None
        self.archive_path = archive_path

    def mark_proxy_dead(self, proxy):
        self.disabled[proxy] = time.monotonic() + 120
        client = self.clients.pop(proxy, None)
        if client:
            client.close()

    def next_proxy(self):
        for _ in range(len(self.proxies)):
            proxy = self.proxies[self.cursor % len(self.proxies)]
            self.cursor += 1
            if self.disabled.get(proxy, 0) <= time.monotonic():
                self.disabled.pop(proxy, None)
                return proxy
        time.sleep(min(30, max(0, min(self.disabled.values()) - time.monotonic())))
        return self.next_proxy()

    def retry_pause(self, attempt, proxy, reason, retry_after=''):
        pause = self.base_delay * (2 ** attempt)
        if retry_after.isdigit():
            pause = max(pause, min(float(retry_after), 30))
        last = attempt + 1 >= self.retries
        event = dict(attempt=attempt + 1, limit=self.retries, reason=reason,
                     proxy_port=urlsplit(proxy).port, wait_seconds=0 if last else pause)
        print(f'Спроба {attempt + 1}/{self.retries}: {reason}; порт {event["proxy_port"]}; '
              + ('ліміт спроб вичерпано.' if last else f'зміна проксі, пауза {pause:g} с.'), file=sys.stderr)
        if self.on_event:
            self.on_event(event)
        if not last:
            time.sleep(pause)

    def get_json(self, url, params=None):
        for attempt in range(self.retries):
            proxy = self.next_proxy()
            if proxy not in self.clients:
                if len(self.clients) >= 16:
                    self.clients.popitem(last=False)[1].close()
                self.clients[proxy] = self.client_factory(
                    proxy=proxy, verify=False, timeout=self.timeout, trust_env=False,
                    headers={'Accept': 'application/json, text/plain, */*',
                             'User-Agent': 'ArshinEquipmentParser/1.0',
                             'Referer': BASE + '/results'})
            self.clients.move_to_end(proxy)
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            self.request_count += 1
            try:
                response = self.clients[proxy].get(url, params=params)
                if self.archive_path:
                    from collector_archive import archive_response
                    archive_response(self.archive_path, response)
                if response.status_code in TRANSIENT_STATUS_CODES:
                    if response.status_code in (429, 499):
                        self.mark_proxy_dead(proxy)
                    self.retry_pause(attempt, proxy, f'HTTP {response.status_code}', response.headers.get('Retry-After', ''))
                    continue
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError:
                    raise ProtocolError('Замість JSON отримано HTML або іншу відповідь') from None
                if not isinstance(payload, dict) or 'error' in payload:
                    raise ProtocolError('Некоректна JSON-відповідь АРШИН')
                return payload
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (404, 405):
                    raise ProtocolError(f'Ендпоінт повернув HTTP {exc.response.status_code}') from None
                if exc.response.status_code not in (403, 408, 500):
                    raise ProtocolError(f'HTTP {exc.response.status_code}') from None
                self.retry_pause(attempt, proxy, f'HTTP {exc.response.status_code}')
            except httpx.TransportError as exc:
                self.mark_proxy_dead(proxy)
                self.retry_pause(attempt, proxy, type(exc).__name__)
        raise RuntimeError(f'Запит не виконано після {self.retries} спроб; адреси проксі приховано')

    def close(self):
        for client in self.clients.values():
            client.close()


def unpack_search(payload):
    if isinstance(payload.get('response'), dict):
        block, items_key, count_key = payload['response'], 'docs', 'numFound'
    elif isinstance(payload.get('result'), dict):
        block, items_key, count_key = payload['result'], 'items', 'count'
    else:
        raise ProtocolError('Невідомий формат пошукової відповіді')
    items, count = block.get(items_key), block.get(count_key)
    if not isinstance(items, list) or not isinstance(count, int) or count < 0:
        raise ProtocolError('У пошуковій відповіді немає коректних записів/лічильника')
    if any(not isinstance(item, dict) or not clean(item.get('vri_id')) for item in items):
        raise ProtocolError('Пошуковий запис без vri_id')
    return items, count


def owner_identity(info, targets):
    """Match owner/customer only; never verifier organization or calibration means."""
    owner = info.get('miOwner')
    if isinstance(owner, dict):
        name = clean(owner.get('name') or owner.get('title'))
        explicit_inn = clean(owner.get('inn'))
    else:
        name, explicit_inn = clean(owner), None
    explicit_inn = explicit_inn or clean(info.get('miOwnerInn'))
    inns = set(re.findall(r'(?<!\d)\d{10}(?:\d{2})?(?!\d)', name or ''))
    if explicit_inn:
        inns.add(explicit_inn)
    if inns:
        matches = inns.intersection(targets)
        if len(inns) == 1 and len(matches) == 1:
            return name, next(iter(matches))
        return None
    # Aliases are an explicit user-supplied INN→name mapping, not guessed identities.
    matches = [inn for inn, aliases in targets.items()
               if name and normalized_name(name) in {normalized_name(a) for a in aliases}]
    return (name, matches[0]) if len(matches) == 1 else None


def date_iso(value):
    text = clean(value)
    if text:
        for fmt, candidate in [('%Y-%m-%d', text[:10]), ('%d.%m.%Y', text[:10])]:
            try:
                return datetime.strptime(candidate, fmt).date().isoformat()
            except ValueError:
                pass
    return None


def parse_card(doc, payload, targets):
    result = payload.get('result')
    if not isinstance(result, dict) or not isinstance(result.get('vriInfo'), dict):
        raise ProtocolError('Картка не містить result.vriInfo')
    info = result['vriInfo']
    identity = owner_identity(info, targets)
    if not identity:
        return None
    # A party/standard is not a single identifiable production instrument.
    mi_info = result.get('miInfo')
    if not isinstance(mi_info, dict):
        raise ProtocolError('Картка не містить коректного result.miInfo')
    mi = mi_info.get('singleMI')
    if not isinstance(mi, dict):
        return None
    title = clean(mi.get('mitypeTitle')) or clean(doc.get('mi.mititle') or doc.get('mit_title'))
    equipment_text = ' '.join(filter(None, (title, clean(mi.get('mitypeType')), clean(mi.get('modification')))))
    if not EQUIPMENT.search(equipment_text):
        return None
    if 'inapplicable' in info and info['inapplicable'] is not None:
        suitable = False
    elif 'applicable' in info and isinstance(info['applicable'], dict):
        suitable = True
    else:
        suitable = doc.get('applicability')
    date = date_iso(info.get('vrfDate')) or date_iso(doc.get('verification_date'))
    if not date or type(suitable) is not bool:
        raise ProtocolError('Картка цільового обладнання без дати або результату повірки')
    return dict(facility_name=identity[0], facility_inn=identity[1], verification_date=date,
                equipment_type=title, equipment_modification=clean(mi.get('modification')),
                equipment_serial_number=clean(mi.get('manufactureNum')),
                verification_result='придатний' if suitable else 'непридатний',
                vri_id=str(doc['vri_id']))


def equipment_query():
    return ' OR '.join(f'{field}:*{marker}*' for field in
                       ('mi.mititle', 'mi.mitype', 'mi.modification') for marker in MARKERS)


def record_ids(path):
    """Validate saved evidence before appending or merging it."""
    ids = set()
    if path.exists():
        with path.open(encoding='utf-8') as stream:
            for line in stream:
                if line.strip():
                    record = json.loads(line)
                    if not isinstance(record, dict) or not clean(record.get('vri_id')):
                        raise ValueError('Збережений JSONL містить запис без vri_id')
                    ids.add(str(record['vri_id']))
    return ids


def merge_records(output, partial):
    """Keep previous output and partial evidence, deduplicated by vri_id."""
    merged = output.with_name(output.name + '.merging')
    ids = set()
    with merged.open('w', encoding='utf-8') as destination:
        for path in (output, partial):
            if not path.exists():
                continue
            with path.open(encoding='utf-8') as source:
                for line in source:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    vri_id = str(record['vri_id'])
                    if vri_id not in ids:
                        destination.write(json.dumps(record, ensure_ascii=False) + '\n')
                        ids.add(vri_id)
        destination.flush()
    merged.replace(output)
    partial.unlink(missing_ok=True)
    return len(ids)


def collect(session, targets, output, search_url=SEARCH_URL, rows=100, max_pages=None,
            detail_url=DETAIL_URL, year=None):
    """Append records immediately; persist checked cards/offset and live counters."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + '.partial')
    report = output.with_name(output.name + '.report.json')
    ledger = sqlite3.connect(str(output) + '.state.sqlite3')
    ledger.executescript('CREATE TABLE IF NOT EXISTS checked(scope TEXT, vri_id TEXT, PRIMARY KEY(scope,vri_id)); CREATE TABLE IF NOT EXISTS progress(scope TEXT PRIMARY KEY, offset INTEGER);')
    scope = hashlib.sha256(json.dumps(dict(targets=targets, query=equipment_query(), year=year, search_url=search_url), sort_keys=True).encode()).hexdigest()
    offset_row = ledger.execute('SELECT offset FROM progress WHERE scope=?', (scope,)).fetchone()
    initial_start = offset_row[0] if offset_row else 0
    stats = dict(run_id=str(uuid4()), started_at=datetime.now(timezone.utc).isoformat(),
                 status='running', pages=0, cards=0, matched=0, total_candidates=None,
                 complete=False, target_inns=list(targets), search_url=search_url, initial_start=initial_start, next_start=initial_start, rows=rows, scope_sha256=scope)

    def checkpoint(event=None):
        if event is not None:
            stats['last_retry'] = event
        stats['requests'] = getattr(session, 'request_count', 0)
        stats['checked_cards_cumulative'] = ledger.execute('SELECT count(*) FROM checked WHERE scope=?', (scope,)).fetchone()[0]
        stats['saved_records'] = len(record_ids(output))
        stats['pagination_basis'] = 'Live index with offset pagination; not a frozen historical snapshot. New or deleted index entries may shift offsets.'
        stats['updated_at'] = datetime.now(timezone.utc).isoformat()
        temporary = report.with_name(report.name + '.tmp')
        temporary.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(report)

    # Replace the old test report immediately; never reuse its counters.
    checkpoint()
    previous_callback = getattr(session, 'on_event', None)
    session.on_event = checkpoint
    seen = set()
    start = initial_start
    try:
        if partial.exists():
            merge_records(output, partial)
        saved = record_ids(output)
        stats['previously_saved'] = len(saved)
        with output.open('a', encoding='utf-8') as stream:
            while max_pages is None or stats['pages'] < max_pages:
                params = {'q': equipment_query(), 'start': start, 'rows': rows,
                          'sort': 'vri_id asc', 'wt': 'json'}
                if year:
                    params['fq'] = f'verification_year:{year}'
                try:
                    docs, count = unpack_search(session.get_json(search_url, params))
                except ProtocolError:
                    if search_url != BASE + '/xcdix/vri':
                        raise
                    search_url = FALLBACK_URL
                    stats['search_url'] = search_url
                    checkpoint()
                    print('xcdix/vri не повернув пошуковий JSON; використовую xcdb/vri/select', file=sys.stderr)
                    docs, count = unpack_search(session.get_json(search_url, params))
                stats['search_url'], stats['total_candidates'] = search_url, count
                stats['pages'] += 1
                checkpoint()
                if not docs and start < count:
                    raise ProtocolError('Порожня сторінка до завершення вибірки')
                fresh = 0
                for doc in docs:
                    vri_id = str(doc['vri_id'])
                    if vri_id in seen:
                        continue
                    seen.add(vri_id)
                    fresh += 1
                    if vri_id in saved or ledger.execute('SELECT 1 FROM checked WHERE scope=? AND vri_id=?', (scope, vri_id)).fetchone():
                        continue
                    payload = session.get_json(detail_url.format(vri_id=quote(vri_id, safe='')))
                    stats['cards'] += 1
                    record = parse_card(doc, payload, targets)
                    if record:
                        evidence_dir = output.with_name(output.name + '.evidence')
                        evidence_dir.mkdir(exist_ok=True)
                        evidence_path = evidence_dir / (hashlib.sha256(vri_id.encode()).hexdigest() + '.json')
                        evidence_path.write_text(json.dumps(dict(search_document=doc, card=payload, source_url=detail_url.format(vri_id=quote(vri_id,safe='')), fetched_at=datetime.now(timezone.utc).isoformat(), user_aliases=targets), ensure_ascii=False) + '\n', encoding='utf-8')
                        record.update(input_sha256=hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
                                      collected_at=json.loads(evidence_path.read_text())['fetched_at'],
                                      source_url=detail_url.format(vri_id=quote(vri_id, safe='')))
                        stream.write(json.dumps(record, ensure_ascii=False) + '\n')
                        stream.flush()
                        saved.add(vri_id)
                        stats['matched'] += 1
                    with ledger:
                        ledger.execute('INSERT OR IGNORE INTO checked VALUES (?,?)', (scope,vri_id))
                    checkpoint()
                    if stats['cards'] % 10 == 0:
                        print(f'Карток: {stats["cards"]}; нових збігів: {stats["matched"]}', file=sys.stderr)
                start += len(docs)
                stats['next_start'] = start
                stats['completed_pages_cumulative'] = start // rows
                with ledger:
                    ledger.execute('INSERT OR REPLACE INTO progress VALUES (?,?)', (scope,start))
                print(f'Карток: {stats["cards"]}; збігів: {stats["matched"]}; кандидатів: {count}', file=sys.stderr)
                if start >= count:
                    stats['complete'] = True
                    break
                if not fresh:
                    raise ProtocolError('Пагінація повторює вже отримані записи')
                checkpoint()
        stats['saved_records'] = len(saved)
        stats['checked_cards_cumulative'] = ledger.execute('SELECT count(*) FROM checked WHERE scope=?', (scope,)).fetchone()[0]
        stats['status'] = 'completed' if stats['complete'] else 'partial'
        checkpoint()
        return stats
    except (Exception, KeyboardInterrupt) as exc:
        stats['status'] = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed'
        stats['complete'] = False
        stats['saved_records'] = len(record_ids(output))
        stats['checked_cards_cumulative'] = ledger.execute('SELECT count(*) FROM checked WHERE scope=?', (scope,)).fetchone()[0]
        stats['error_type'] = type(exc).__name__
        if isinstance(exc, RuntimeError):
            stats['error'] = str(exc)
        checkpoint()
        raise
    finally:
        session.on_event = previous_callback
        ledger.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inns', nargs='+', default=DEFAULT_INNS)
    parser.add_argument('--facility-names', type=Path, help='JSON: {"ІПН": ["точна назва", "аліас"]}')
    parser.add_argument('--proxies', type=Path, default=Path('config/private/proxies.txt'))
    parser.add_argument('--output', type=Path, default=Path('data/processed/equipment/verified_equipment_arshin.jsonl'))
    parser.add_argument('--search-url', default=SEARCH_URL)
    parser.add_argument('--detail-url', default=DETAIL_URL)
    parser.add_argument('--rows', type=int, default=100)
    parser.add_argument('--max-pages', type=int, help='Обмеження для пробного/часткового збору')
    parser.add_argument('--year', type=int)
    parser.add_argument('--timeout', type=float, default=15, help='Таймаут з’єднання/запису')
    parser.add_argument('--proxy-start-port', type=int, help='Start rotation at a previously working port')
    parser.add_argument('--read-timeout', type=float, default=50)
    parser.add_argument('--retries', type=int, default=5)
    parser.add_argument('--delay', type=float, default=0.5)
    args = parser.parse_args(argv)
    if not 1 <= args.rows <= 100 or args.retries < 1 or args.timeout <= 0 or args.read_timeout <= 0 or args.delay < 0 or (args.max_pages is not None and args.max_pages < 1):
        parser.error('Некоректні межі rows/retries/timeout/delay/max-pages')
    if any(not re.fullmatch(r'\d{10}|\d{12}', inn) for inn in args.inns):
        parser.error('ІПН має містити 10 або 12 цифр')
    session = None
    import fcntl
    args.output.parent.mkdir(parents=True, exist_ok=True)
    run_lock = args.output.with_name(args.output.name + '.lock').open('a')
    try:
        fcntl.flock(run_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        run_lock.close()
        parser.error('Інший збирач уже використовує цей вихідний файл')
    try:
        aliases = json.loads(args.facility_names.read_text(encoding='utf-8')) if args.facility_names else {}
        if not isinstance(aliases, dict) or any(not isinstance(v, list) or any(not isinstance(a, str) or not normalized_name(a) for a in v) for v in aliases.values()):
            raise ValueError('Назви мають формат {"ІПН": ["назва", ...]}')
        targets = {inn: aliases.get(inn, []) for inn in args.inns}
        if not any(targets.values()):
            print('Назви заводів не задано: приймаю лише явний ІПН власника в картці.', file=sys.stderr)
        proxies = load_proxies(args.proxies)
        if args.proxy_start_port:
            split = next((i for i,p in enumerate(proxies) if urlsplit(p).port == args.proxy_start_port), 0)
            proxies = proxies[split:] + proxies[:split]
        session = ProxySession(proxies, args.timeout, args.retries, args.delay,
                               read_timeout=min(50, args.read_timeout),
                               archive_path=args.output.with_name(args.output.name + ".responses.jsonl"))
        stats = collect(session, targets, args.output, args.search_url, args.rows,
                        args.max_pages, args.detail_url, args.year)
        print(json.dumps(stats, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        # Do not print exception messages containing proxy credentials or transport URLs.
        print('Збір перервано: перевірте доступ, проксі та формат відповіді. Вже записані рядки збережені у JSONL; прогрес — у .state.sqlite3.', file=sys.stderr)
        if isinstance(exc, RuntimeError):
            print(str(exc), file=sys.stderr)
        return 1
    finally:
        if session:
            session.close()
        run_lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
