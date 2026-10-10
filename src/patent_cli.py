"""Collect public patent publications, cache originals, extract auditable JSONL."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from parsers.patent_parser import (IPC_PREFIXES, TARGETS, from_json, ipc_codes,
                                   parse_html, parse_xml, records)


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def search_plan():
    return [{'assignee': alias, 'canonical_assignee': name, 'ipc_prefixes': list(IPC_PREFIXES),
             # Broad assignee discovery avoids excluding patents missing CPC metadata.
             'url': 'https://patents.google.com/?' + urlencode({'assignee': alias, 'country': 'RU', 'groupby': 'none'}),
             'classification_search_urls': ['https://patents.google.com/?' + urlencode({'assignee': alias, 'country': 'RU', 'cpc': prefix, 'groupby': 'none'}) for prefix in IPC_PREFIXES],
             'prefix_query_example': f'assignee:"{alias}"',
             'classification_policy': 'verify IPC on publication; do not substitute CPC'}
            for name, aliases in TARGETS.items() for alias in aliases]


class Client:
    def __init__(self, cache, delay=1.0):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.delay = delay
        self.last = 0.0
        self.http = httpx.Client(timeout=35, follow_redirects=True, headers={'User-Agent': 'PatentEvidenceParser/1.0'})

    def get(self, url):
        host = urlparse(url).hostname or ''
        if urlparse(url).scheme != 'https' or not (host == 'patents.google.com' or host == 'patentimages.storage.googleapis.com' or host == 'fips.ru' or host.endswith('.fips.ru')):
            raise ValueError('Only HTTPS Google Patents, patentimages and FIPS URLs are allowed')
        key = hashlib.sha256(url.encode()).hexdigest()
        path = self.cache / (key + '.bin')
        metadata = self.cache / (key + '.json')
        if path.exists() and metadata.exists():
            return path.read_bytes(), json.loads(metadata.read_text())
        for attempt in range(3):
            time.sleep(max(0, self.delay - (time.monotonic() - self.last)))
            try:
                response = self.http.get(url)
            finally:
                self.last = time.monotonic()
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(min(2 ** attempt, 4))
                continue
            response.raise_for_status()
            if len(response.content) > 32 * 1024 * 1024:
                raise ValueError('Document exceeds 32 MiB limit')
            content = response.content
            if not content.startswith(b'%PDF'):
                text = content[:200000].decode(response.encoding or 'utf-8', errors='replace').lower()
                if any(marker in text for marker in ('ddos-guard', 'verify you are human', 'captcha', 'access denied')):
                    raise ValueError('Access challenge; saved official document required')
            meta = {'source_url': url, 'resolved_url': str(response.url), 'sha256': hashlib.sha256(content).hexdigest(),
                    'encoding': response.encoding or 'utf-8', 'content_type': response.headers.get('content-type', '')}
            path.write_bytes(content)
            dump(metadata, meta)
            return content, meta
        raise RuntimeError('Retry exhausted')

    def close(self):
        self.http.close()


def verify_pdf_ipc(patent, client):
    """Read IPC only from first-page INID (51), never from cited patents."""
    if patent.ipc or not patent.pdf_url:
        return
    import re
    from pypdf import PdfReader
    data, meta = client.get(patent.pdf_url)
    reader = PdfReader(io.BytesIO(data))
    text = reader.pages[0].extract_text() or ''
    field = re.search(r'\(51\)\s*(?:МПК|Int\.?\s*Cl\.?|IPC)?(.*?)(?=\(\d{2}\)|$)', text, re.S | re.I)
    if field:
        patent.ipc = ipc_codes(field[1])
    return meta


def google_discover(client, plan, pages, limit, audit):
    urls = []
    seen = set()
    queries = [(query, prefix) for query in plan for prefix in query['ipc_prefixes']] + [(query, None) for query in plan]
    for query, prefix in queries:
        for page in range(pages):
            parameters = {'assignee': query['assignee'], 'country': 'RU', 'num': 100, 'page': page, 'groupby': 'none'}
            if prefix:
                parameters['cpc'] = prefix
            params = urlencode(parameters)
            url = 'https://patents.google.com/xhr/query?' + urlencode({'url': params, 'exp': ''})
            try:
                data, _ = client.get(url)
                result = json.loads(data)['results']
                if 'cluster' not in result:
                    raise ValueError('Unexpected Google search response')
                audit.append({'url': url, 'status': 'discovered', 'total': result.get('total_num_results'),
                              'total_pages': result.get('total_num_pages'), 'page': page,
                              'candidate_cpc_prefix': prefix,
                              'truncated': int(result.get('total_num_pages', 0)) > pages})
                for cluster in result['cluster']:
                    for entry in cluster.get('result', []):
                        number = entry.get('patent', {}).get('publication_number')
                        import re
                        if not number or not re.fullmatch(r'RU\d+[A-Z]\d?', number):
                            continue
                        if number not in seen:
                            seen.add(number)
                            urls.append('https://patents.google.com/patent/' + number + '/ru')
                        if len(urls) >= limit:
                            audit.append({'status': 'collection_limit_reached', 'limit': limit})
                            return urls
                if page + 1 >= int(result.get('total_num_pages', 0)):
                    break
            except Exception as exc:
                audit.append({'url': url, 'status': 'discovery_error', 'error': str(exc)})
                break
    return urls


def parse_content(content, name, url=''):
    suffix = Path(name).suffix.lower()
    if suffix in ('.json', '.jsonl'):
        text = content.decode('utf-8-sig')
        rows = [json.loads(line) for line in text.splitlines() if line.strip()] if suffix == '.jsonl' else json.loads(text)
        rows = rows if isinstance(rows, list) else [rows]
        return [from_json(row) for row in rows]
    if suffix == '.xml' or content.lstrip().startswith(b'<?xml'):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(content)
        documents = [node for node in root.iter() if node.tag.split('}')[-1] in ('patent-document', 'ru-patent-document')]
        return [parse_xml(ET.tostring(node), url) for node in documents] if documents else [parse_xml(content, url)]
    soup = BeautifulSoup(content, 'html.parser')
    return [parse_html(str(soup), url)]


def run(args):
    audit, out, seen = [], [], set()
    client = Client(args.cache, args.delay)
    def consume(content, name, url='', meta=None):
        try:
            patents = parse_content(content, name, url)
            for patent in patents:
                if args.command == 'collect' and patent.pdf_url and not patent.ipc:
                    try:
                        pdf_meta = verify_pdf_ipc(patent, client)
                        audit.append({'patent_number': patent.patent_number, 'status': 'ipc_pdf_check', 'metadata': pdf_meta})
                    except Exception as exc:
                        audit.append({'patent_number': patent.patent_number, 'status': 'ipc_pdf_error', 'error': str(exc)})
                extracted, status = records(patent, args.ipc)
                audit.append({'input': name, 'url': url, 'patent_number': patent.patent_number,
                              'status': status, 'records': len(extracted), 'metadata': meta})
                for row in extracted:
                    key = (row['patent_number'], row['assignee_name'], row['section'], row['raw_snippet_quote'])
                    if key not in seen:
                        seen.add(key)
                        out.append(row)
        except Exception as exc:
            audit.append({'input': name, 'url': url, 'status': 'parse_error', 'error': str(exc)})
    try:
        if args.command == 'import':
            for path in args.inputs:
                p = Path(path)
                if not p.exists():
                    audit.append({'input': str(p), 'status': 'parse_error', 'error': 'Input does not exist'})
                    continue
                paths = sorted(p.rglob('*')) if p.is_dir() else [p]
                for file in paths:
                    if file.is_file() and file.suffix.lower() in ('.json', '.jsonl', '.html', '.htm', '.xml'):
                        try:
                            consume(file.read_bytes(), str(file), file.resolve().as_uri())
                        except OSError as exc:
                            audit.append({'input': str(file), 'status': 'parse_error', 'error': str(exc)})
                    elif file == p and file.is_file():
                        audit.append({'input': str(file), 'status': 'parse_error', 'error': 'Unsupported input extension'})
        else:
            urls = list(args.url or [])
            if args.csv:
                with open(args.csv, encoding='utf-8-sig', newline='') as file:
                    # Google CSV may prepend a search URL before its header.
                    lines = list(file)
                    start = next((i for i, line in enumerate(lines) if 'publication number' in line.lower()), 0)
                    for row in csv.DictReader(lines[start:]):
                        normalized = {k.strip().lower(): v for k, v in row.items() if k}
                        number = normalized.get('publication number', '').replace('-', '')
                        import re
                        if re.fullmatch(r'RU\d+[A-Z]\d?', number):
                            urls.append('https://patents.google.com/patent/' + number + '/ru')
            if args.source == 'google' and not urls:
                plan = [q for q in search_plan() if not args.assignee or q['canonical_assignee'] in args.assignee]
                for query in plan:
                    query['ipc_prefixes'] = args.ipc
                urls = google_discover(client, plan, args.pages, args.limit, audit)
            if args.source == 'fips' and not urls:
                urls = ['https://fips.ru/elektronnye-byulleteni/']
            queue = deque((url, 0) for url in dict.fromkeys(urls))
            visited = set()
            while queue and len(visited) < args.limit:
                url, depth = queue.popleft()
                if url in visited:
                    continue
                visited.add(url)
                try:
                    content, meta = client.get(url)
                    if content.startswith(b'%PDF'):
                        audit.append({'url': url, 'status': 'pdf_requires_bibliographic_html_or_xml'})
                        continue
                    consume(content, urlparse(url).path, url, meta)
                    if args.source == 'fips' and depth < args.depth:
                        soup = BeautifulSoup(content, 'html.parser')
                        for a in soup.select('a[href]'):
                            link = urljoin(url, a['href']).split('#')[0]
                            parsed = urlparse(link)
                            host = parsed.hostname or ''
                            if parsed.scheme == 'https' and (host == 'fips.ru' or host.endswith('.fips.ru')) and link not in visited:
                                if not Path(parsed.path).suffix.lower() in ('.jpg', '.png', '.css', '.js', '.zip', '.pdf'):
                                    queue.append((link, depth + 1))
                except Exception as exc:
                    audit.append({'url': url, 'status': 'download_error', 'error': str(exc)})
            if queue:
                audit.append({'status': 'collection_limit_reached', 'remaining_urls': len(queue)})
    finally:
        client.close()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in out), encoding='utf-8')
    summary = {'records': len(out), 'calculated_records': sum(bool(r['calculated_component_multiplier']) for r in out),
               'events': audit, 'output': str(output.resolve()), 'coverage': 'bounded_run_not_complete_registry'}
    dump(args.audit or str(output) + '.audit.json', summary)
    print(json.dumps({k: v for k, v in summary.items() if k != 'events'}, ensure_ascii=False))
    return 1 if any(e['status'] in ('download_error', 'discovery_error', 'parse_error', 'ipc_pdf_error') for e in audit) else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Патенти → технічні фрагменти та умовні множники компонентів')
    sub = parser.add_subparsers(dest='command', required=True)
    plan = sub.add_parser('plan', help='Запити за всіма варіантами назв власників')
    plan.add_argument('--output', default='outputs/patents/search_plan.json')
    for command in ('import', 'collect'):
        p = sub.add_parser(command)
        p.add_argument('--output', required=True)
        p.add_argument('--audit')
        p.add_argument('--cache', default='data/patent_cache')
        p.add_argument('--delay', type=float, default=1.0)
        p.add_argument('--ipc', nargs='+', choices=IPC_PREFIXES, default=list(IPC_PREFIXES))
        if command == 'import':
            p.add_argument('inputs', nargs='+')
        else:
            p.add_argument('--source', choices=('google', 'fips'), default='google')
            p.add_argument('--url', action='append')
            p.add_argument('--csv', help='Google Patents CSV export')
            p.add_argument('--assignee', action='append', choices=list(TARGETS))
            p.add_argument('--pages', type=int, default=1)
            p.add_argument('--limit', type=int, default=50)
            p.add_argument('--depth', type=int, default=2)
    args = parser.parse_args(argv)
    if args.command == 'plan':
        dump(args.output, search_plan())
        return 0
    if args.delay < 0 or (args.command == 'collect' and (args.pages < 1 or args.limit < 1 or args.depth < 0)):
        parser.error('delay/depth must be nonnegative; pages/limit must be positive')
    return run(args)


if __name__ == '__main__':
    raise SystemExit(main())
