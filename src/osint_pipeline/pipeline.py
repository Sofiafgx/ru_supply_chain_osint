"""Manifest runner, streaming NDJSON sink, and evidence-backed shipment intervals."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import quote, urljoin
from lxml.etree import LxmlError

from osint_pipeline.adapters import SOURCES, parse
from osint_pipeline.schema import FIELDS, normalize, utc
from osint_pipeline.transport import Archive, CollectionError, Deferred, Transport, now, public_url

ENDPOINTS = {
    'hh': 'https://api.hh.ru/vacancies',
    'trudvsem': 'https://opendata.trudvsem.ru/api/v1/vacancies',
    'arshin': 'https://fgis.gost.ru/fundmetrology/cm/xcdb/vri/select',
    'comtrade': 'https://comtradeapi.un.org/public/v1/preview/C/M/HS',
    'copernicus': 'https://catalogue.dataspace.copernicus.eu/odata/v1/Products',
    'zakupki': 'https://zakupki.gov.ru/epz/contract/search/results.html',
    'clearspending': 'https://clearspending.ru/api/gw/filtered-contracts/',
    'kad': 'https://kad.arbitr.ru/Kad/SearchInstances',
}


def jsonline(stream, value):
    stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n')
    stream.flush()


class Sink:
    def __init__(self, output):
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.stack = ExitStack()
        self.streams = {block: self.stack.enter_context((self.output / f'block_{block}.ndjson').open('a', encoding='utf-8')) for block in SOURCES}
        self.db = sqlite3.connect(self.output / 'shipment_intervals.sqlite3')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS arrivals (
            importer_inn TEXT, hs_code TEXT, day TEXT, input_sha256 TEXT, source_url TEXT,
            PRIMARY KEY(importer_inn, hs_code, day, input_sha256, source_url));''')

    def write(self, block, data, evidence, job):
        row = normalize(block, data)
        url = row.pop('source_url', None) or evidence['source_url']
        if url:
            url = public_url(urljoin(evidence['source_url'] or '', url))
        row.update(source_url=url, input_sha256=evidence['input_sha256'], collected_at=evidence['collected_at'],
                   raw_file=evidence['raw_file'], source=job['source'], job_id=job['id'],
                   ingested_at=now())
        if evidence['collected_at'] is None:
            row['warnings'].append('primary_collection_time_unknown')
        if url is None:
            row['warnings'].append('primary_source_url_unknown')
        jsonline(self.streams[block], row)
        if block == 4 and row.get('record_kind') == 'shipment' and all(row.get(k) for k in ('importer_inn', 'hs_code', 'declaration_date', 'source_url')):
            self.db.execute('INSERT OR IGNORE INTO arrivals VALUES (?,?,?,?,?)',
                            tuple(row[k] for k in ('importer_inn', 'hs_code', 'declaration_date', 'input_sha256', 'source_url')))
            self.db.commit()
        return row

    def intervals(self):
        with (self.output / 'shipment_intervals.ndjson').open('w', encoding='utf-8') as stream:
            groups = self.db.execute('SELECT DISTINCT importer_inn,hs_code FROM arrivals ORDER BY importer_inn,hs_code')
            from datetime import date
            for tax_id, code in groups:
                previous = None
                dates = self.db.execute('SELECT DISTINCT day FROM arrivals WHERE importer_inn=? AND hs_code=? ORDER BY day', (tax_id, code))
                for (day,) in dates:
                    if previous:
                        evidence = [dict(input_sha256=r[0], source_url=r[1], declaration_date=r[2]) for r in self.db.execute(
                            'SELECT input_sha256,source_url,day FROM arrivals WHERE importer_inn=? AND hs_code=? AND day IN (?,?)', (tax_id, code, previous, day))]
                        jsonline(stream, dict(importer_inn=tax_id, hs_code=code, previous_date=previous,
                                              declaration_date=day, interval_days=(date.fromisoformat(day) - date.fromisoformat(previous)).days,
                                              evidence=evidence, metric_kind='observed_shipment_date_interval'))
                    previous = day

    def close(self):
        self.db.close()
        self.stack.close()


async def responses(job, client, state):
    """Yield (response, format, parser job). Pagination bounds are always reported."""
    mode, source = job.get('mode', 'url'), job['source']
    params = dict(job.get('params', {}))
    if mode == 'url':
        response = await client.request(job.get('method', 'GET'), job['url'], params=params,
                                        **({'json': job['body']} if 'body' in job else {}))
        state['pages'] += 1
        state['complete'] = None  # A supplied page is not proof of complete source history.
        yield response, job.get('format', infer_format(response)), job
        return
    if mode != 'search':
        raise CollectionError('unsupported_mode')
    max_pages = job.get('max_pages', 1)
    page_size = job.get('page_size', 25)
    if not isinstance(max_pages, int) or not 1 <= max_pages <= 1000 or not isinstance(page_size, int) or not 1 <= page_size <= 100:
        raise CollectionError('invalid_page_limits')
    if source == 'firms':
        env = job.get('key_env', 'FIRMS_MAP_KEY')
        key = os.environ.get(env)
        if not key:
            raise CollectionError('missing_environment_credential:' + env)
        area = job.get('area', 'world')
        days = job.get('day_range', 1)
        if not isinstance(days, int) or not 1 <= days <= 10:
            raise CollectionError('firms_day_range_must_be_1_to_10')
        url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{quote(key, safe='')}/{quote(job.get('sensor', 'VIIRS_SNPP_NRT'), safe='')}/{quote(str(area), safe=',.-')}/{days}"
        if job.get('date'):
            url += '/' + quote(job['date'], safe='-')
        response = await client.request('GET', url, secrets=[key])
        state.update(pages=1, complete=True)
        yield response, 'csv', job
        return
    if source not in ENDPOINTS:
        raise CollectionError('search_unavailable_use_direct_page_or_export')
    endpoint = ENDPOINTS[source]
    next_url = None
    if source == 'kad':
        await client.request('GET', 'https://kad.arbitr.ru/Version/Change?mode=Full&returnUrl=%2F')
    for page in range(max_pages):
        query = dict(params)
        if source == 'hh':
            query.update(text=job.get('query', ''), page=page, per_page=page_size)
        elif source == 'trudvsem':
            query.update(offset=page * page_size, limit=page_size)
            if job.get('query'):
                query['text'] = job['query']
        elif source == 'arshin':
            query.update(q=job.get('query', '*:*'), start=page * page_size, rows=page_size, wt='json', sort='vri_id asc')
        elif source == 'copernicus':
            query.update({'$top': page_size, '$expand': 'Attributes'})
        elif source == 'zakupki':
            query.update(searchString=job.get('query', ''), pageNumber=page + 1, recordsPerPage='_10', searchResultView='GENERAL')
        elif source == 'clearspending':
            query.update(search=job.get('query', ''), page=page + 1, page_size=page_size)
        elif source == 'comtrade':
            if page:
                break  # Preview has a result cap, no assumed offset protocol.
            query.setdefault('maxRecords', 500)
        if source == 'kad':
            body = dict(Page=page + 1, Count=page_size, Sides=[dict(Name=job.get('query', ''), Type=-1, ExactMatch=False)],
                        Judges=[], Courts=[], CaseNumbers=[], DateFrom=None, DateTo=None, WithVKSInstances=False)
            response = await client.request('POST', endpoint, json=body, headers={'x-date-format': 'iso', 'Referer': 'https://kad.arbitr.ru/'})
        else:
            response = await client.request('GET', next_url or endpoint, params=None if next_url else query)
        state['pages'] += 1
        if source in ('zakupki', 'kad'):
            if source == 'zakupki':
                from bs4 import BeautifulSoup
                from urllib.parse import urlsplit
                from parsers.source_common import soup_html
                soup = soup_html(response.text)
                links = list(dict.fromkeys(urljoin(str(response.url), a['href']) for a in soup.select('a[href*="reestrNumber"], a[href*="regNumber"]')))
                for link in links:
                    if urlsplit(link).hostname != 'zakupki.gov.ru':
                        continue
                    detail = await client.request('GET', link)
                    yield detail, 'html', job
                if not links:
                    # Unrecognized HTML never proves no matching contracts.
                    raise CollectionError('no_recognized_contract_links_or_schema_changed')
            else:
                yield response, 'html', job
            continue
        payload = response.json()
        if not isinstance(payload, dict):
            raise CollectionError('unexpected_search_payload')
        if source == 'hh':
            items = payload.get('items')
            if not isinstance(items, list) or not isinstance(payload.get('pages'), int):
                raise CollectionError('hh_schema_changed')
            for item in items:
                if item.get('id') is None:
                    raise CollectionError('hh_missing_vacancy_id')
                detail = await client.request('GET', ENDPOINTS['hh'] + '/' + quote(str(item['id']), safe=''))
                yield detail, 'json', job
            if page + 1 >= payload['pages'] or not items:
                state['complete'] = True
                break
        elif source == 'arshin':
            from arshin_parser import unpack_search
            items, total = unpack_search(payload)
            for item in items:
                detail = await client.request('GET', 'https://fgis.gost.ru/fundmetrology/cm/iaux/vri/' + quote(str(item['vri_id']), safe=''))
                detail_job = dict(job, detail_vri_id=str(item['vri_id']))
                yield detail, 'json', detail_job
            if (page + 1) * page_size >= total:
                state['complete'] = True
                break
        elif source == 'copernicus':
            if not isinstance(payload.get('value'), list):
                raise CollectionError('copernicus_schema_changed')
            yield response, 'json', job
            next_url = payload.get('@odata.nextLink')
            if not next_url:
                state['complete'] = True
                break
            if not next_url.startswith(ENDPOINTS[source]):
                raise CollectionError('unexpected_pagination_destination')
        elif source == 'comtrade':
            if not isinstance(payload.get('data'), list) or payload.get('error'):
                raise CollectionError('comtrade_schema_or_api_error')
            yield response, 'json', job
            state['complete'] = False if len(payload['data']) >= int(query['maxRecords']) else None
            state['scope_note'] = 'preview_aggregate_response; completeness_not_proven'
            break
        elif source == 'clearspending':
            if not isinstance(payload.get('data'), list):
                raise CollectionError('clearspending_schema_changed')
            yield response, 'json', job
            if not payload['data'] or isinstance(payload.get('total_pages'), int) and page + 1 >= payload['total_pages']:
                state['complete'] = True
                break
        else:
            from osint_pipeline.adapters import objects
            items = objects(payload)
            yield response, 'json', job
            if len(items) < page_size:
                state['complete'] = True
                break


def infer_format(response):
    content_type = response.headers.get('content-type', '').casefold()
    if 'json' in content_type:
        return 'json'
    if 'csv' in content_type:
        return 'csv'
    if 'xml' in content_type:
        return 'xml'
    return 'html'


def validate(config):
    jobs = config.get('jobs')
    if not isinstance(jobs, list) or not jobs:
        raise ValueError('Manifest needs a nonempty jobs list')
    ids = set()
    for job in jobs:
        if not isinstance(job, dict) or not isinstance(job.get('id'), str) or not job['id'] or job['id'] in ids:
            raise ValueError('Job IDs must be unique nonempty strings')
        ids.add(job['id'])
        if job.get('source') not in SOURCES.get(job.get('block'), ()):
            raise ValueError('Invalid source/block for job ' + job['id'])
        mode = job.get('mode', 'url')
        if mode not in ('url', 'file', 'search') or mode == 'file' and not job.get('path') or mode == 'url' and not job.get('url'):
            raise ValueError('Missing input or invalid mode: ' + job['id'])
        if job.get('collected_at') is not None and utc(job['collected_at']) is None:
            raise ValueError('collected_at must be a full timestamp with timezone')
        for key in ('column_map', 'selectors'):
            if key in job and (not isinstance(job[key], dict) or set(job[key]) - set(FIELDS[job['block']]) - {'source_url'}):
                raise ValueError('Unknown field in ' + key)
        for key in ('url', 'source_url'):
            if job.get(key):
                public_url(job[key])
    return config


async def run(manifest_path, output):
    manifest_path, output = Path(manifest_path).resolve(), Path(output).resolve()
    config = validate(json.loads(manifest_path.read_text(encoding='utf-8')))
    output.mkdir(parents=True, exist_ok=True)
    import fcntl
    with (output / 'pipeline.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CollectionError('output_directory_already_in_use') from None
        sink, archive = Sink(output), Archive(output / 'raw')
        network = dict(config.get('network', {}))
        proxy_file = network.pop('proxy_file', None)
        if proxy_file:
            proxy_path = manifest_path.parent / proxy_file
            network['proxies'] = [line.strip() for line in proxy_path.read_text().splitlines() if line.strip() and not line.lstrip().startswith('#')]
        stats = dict(started_at=now(), jobs=[], output=str(output))
        try:
            async with Transport(archive, **network) as client:
                for job in config['jobs']:
                    state = dict(job_id=job['id'], source=job['source'], block=job['block'], records=0, pages=0, complete=False)
                    try:
                        if job.get('mode') == 'file':
                            path = (manifest_path.parent / job['path']).resolve()
                            raw = path.read_bytes()
                            evidence = archive.save(raw, public_url(job['source_url']) if job.get('source_url') else None,
                                                    utc(job.get('collected_at')), input_file=str(path), imported_at=now())
                            fmt = job.get('format', path.suffix.lstrip('.').lower())
                            for row in parse(raw.decode(job.get('encoding', 'utf-8-sig')), fmt, job, evidence['source_url']):
                                sink.write(job['block'], row, evidence, job)
                                state['records'] += 1
                            state['complete'] = None
                        else:
                            async for response, fmt, parser_job in responses(job, client, state):
                                evidence = response.extensions['evidence']
                                for row in parse(response.text, fmt, parser_job, evidence['source_url']):
                                    if parser_job.get('detail_vri_id') and row.get('vri_id') is None:
                                        row['vri_id'] = parser_job['detail_vri_id']
                                    sink.write(job['block'], row, evidence, job)
                                    state['records'] += 1
                        state['status'] = 'found' if state['records'] else 'no_matching_records_in_parsed_input'
                        if job.get('mode', 'url') == 'search' and state['complete'] is False:
                            state['scope_note'] = state.get('scope_note', 'page_limit_reached; source_history_incomplete')
                    except Deferred as exc:
                        state.update(status='partial_error' if state['records'] else 'technical_access_unavailable',
                                     error=str(exc), retry_after_seconds=exc.retry_after_seconds, complete=False)
                    except (ValueError, OSError, KeyError, TypeError, RuntimeError, LxmlError) as exc:
                        # Avoid secrets in arbitrary parser/HTTP exception strings.
                        state.update(status='partial_error' if state['records'] else 'technical_access_unavailable',
                                     error=str(exc) if isinstance(exc, CollectionError) else type(exc).__name__, complete=False)
                    stats['jobs'].append(state)
                    checkpoint(output, stats)
            sink.intervals()
        finally:
            sink.close()
        stats['finished_at'] = now()
        checkpoint(output, stats)
        return stats


def checkpoint(output, stats):
    temporary = output / 'report.json.tmp'
    temporary.write_text(json.dumps(stats, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(output / 'report.json')


def main():
    import asyncio
    parser = argparse.ArgumentParser(description='Seven-block auditable primary-source OSINT pipeline')
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--output', type=Path, default=Path('outputs/osint_pipeline'))
    parser.add_argument('--validate-only', action='store_true')
    args = parser.parse_args()
    if args.validate_only:
        validate(json.loads(args.manifest.read_text(encoding='utf-8')))
        print('Manifest valid')
        return
    stats = asyncio.run(run(args.manifest, args.output))
    print(json.dumps(dict(output=stats['output'], jobs=len(stats['jobs']), records=sum(j['records'] for j in stats['jobs']),
                          errors=sum(j['status'] in ('partial_error', 'technical_access_unavailable') for j in stats['jobs']))))
    if any(job['status'] in ('partial_error', 'technical_access_unavailable') for job in stats['jobs']):
        raise SystemExit(2)
