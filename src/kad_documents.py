"""Court search queue and evidence-preserving PDF collection; no inferred contract roles."""
import argparse
import collections
import csv
import hashlib
import io
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from supplier_enrichment import valid_inn
from parsers.registry_sources import KadParser


def official_url(url):
    parsed = urlparse(url)
    if (parsed.scheme != 'https' or parsed.hostname not in ('kad.arbitr.ru', 'ras.arbitr.ru')
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError('Only HTTPS court URLs on kad.arbitr.ru or ras.arbitr.ru are accepted')
    return url


def challenge(content):
    lower = content.lower()
    visible = BeautifulSoup(content, 'html.parser').get_text(' ', strip=True).lower()
    if any(term in visible for term in ('доступ заблокирован', 'access denied', 'подтвердите, что вы не робот')):
        return True
    # Full public case cards can load the CAPTCHA library without showing a challenge.
    public_card = 'участники дела' in visible and re.search(r'[аa]\d{1,3}-\d+/\d{4}', visible)
    return 'pravocaptcha' in lower and not public_card


def discover_pdfs(html, card_url):
    official_url(card_url)
    if challenge(html):
        raise ValueError('Court returned an access challenge, not an empty case')
    links = []
    for link in BeautifulSoup(html, 'html.parser').select('a[href]'):
        url = urljoin(card_url, link['href'])
        path = urlparse(url).path.lower()
        # Download only actual published PDF links; search endpoints do not return PDFs.
        if path.endswith('.pdf'):
            official_url(url)
            label = link.get_text(' ', strip=True)
            if 'политик' in label.lower() or '/content/' in path:
                continue
            if url not in links:
                links.append(url)
    return links


def text_evidence(pages):
    findings = []
    patterns = {
        'contract_reference': r'(?i)(?:договор|контракт)[а-я\s]{0,30}(?:№|N)\s*[^\n;,]{1,100}',
        'money_mention': r'\d[\d\s\u00a0]*(?:[.,]\d{2})?\s*(?:руб(?:лей|ля|ль|\.)?|₽)',
        'party_role': r'(?i)(?:истец|ответчик|покупатель|поставщик|заказчик|подрядчик)[^\n]{0,180}',
    }
    for page_number, text in enumerate(pages, 1):
        for kind, pattern in patterns.items():
            for match in re.finditer(pattern, text):
                findings.append({'kind': kind, 'page': page_number, 'quote': match.group(0),
                                 'context': text[max(0, match.start()-100):match.end()+150],
                                 'review_status': 'candidate_requires_review'})
    return findings


def extract_pdf(content, case_id, source_url, output):
    official_url(source_url)
    if not content.startswith(b'%PDF-'):
        raise ValueError('Response is not a PDF')
    reader = PdfReader(io.BytesIO(content))
    pages = [page.extract_text() or '' for page in reader.pages]
    digest = hashlib.sha256(content).hexdigest()
    output.mkdir(parents=True, exist_ok=True)
    path = output / (digest + '.pdf')
    path.write_bytes(content)
    record = {'case_id': case_id, 'source_url': source_url, 'file': str(path.resolve()),
              'sha256': digest, 'pages': [{'page': i, 'text': text} for i, text in enumerate(pages, 1)],
              'findings': text_evidence(pages), 'text_status': 'extracted' if any(t.strip() for t in pages) else 'needs_ocr',
              'contract_execution_status': None,
              'note': 'Money mentions are not automatically claim amounts; procedural roles are not buyer/supplier roles.'}
    path.with_suffix('.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    return record


def build_queue(companies, targets, cases, output):
    companies_by_inn = {}
    # Explicit project targets first, then all legal persons in the existing directory.
    for target_path in targets:
        payload = json.loads(target_path.read_text())
        for row in payload.get('companies', payload.get('entities', [])):
            tax_id = row.get('inn')
            if valid_inn(tax_id) and len(tax_id) == 10:
                names = row.get('names') or [row.get('name')]
                companies_by_inn.setdefault(tax_id, {'inn': tax_id, 'names': names, 'origins': [], 'status': 'pending'})['origins'].append(str(target_path.resolve()))
    with companies.open(encoding='utf-8-sig', newline='') as handle:
        for row in csv.DictReader(handle):
            tax_id = row['inn']
            if valid_inn(tax_id) and len(tax_id) == 10:
                entry = companies_by_inn.setdefault(tax_id, {'inn': tax_id, 'names': json.loads(row['names']), 'origins': [], 'status': 'pending'})
                entry['origins'].append(str(companies.resolve()))
    employers = Path('database/employers_2026-10-09.csv')
    with employers.open(encoding='utf-8-sig', newline='') as handle:
        for row in csv.DictReader(handle):
            tax_id = row.get('inn')
            if valid_inn(tax_id) and len(tax_id) == 10:
                entry = companies_by_inn.setdefault(tax_id, {'inn': tax_id, 'names': [row['name']], 'origins': [], 'status': 'pending'})
                entry['origins'].append(str(employers.resolve()))
    plan = {'period_from': '2022-01-01', 'period_to': '2026-10-08', 'targets': list(companies_by_inn.values()),
            'note': 'Queue only; pending targets have not been searched. Full dictionary coverage is not claimed.'}
    output.mkdir(parents=True, exist_ok=True)
    (output / 'kad_search_queue.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n')
    known_cases = [json.loads(line) for line in cases.read_text().splitlines() if line.strip()]
    (output / 'kad_document_queue.json').write_text(json.dumps(
        [{'case_id': r['record_id'], 'case_number': r['data']['case_number'], 'card_url': r['source_url'],
          'participant_search_inn': r['data'].get('search_participant_inn'), 'status': 'pending', 'pdf_urls': []}
         for r in known_cases], ensure_ascii=False, indent=2) + '\n')
    return {'legal_person_search_targets': len(companies_by_inn), 'known_cases': len(known_cases)}


def get(client, url):
    # Validate every redirect before making the next request.
    for _ in range(6):
        response = client.get(official_url(url))
        if response.is_redirect:
            url = urljoin(url, response.headers['location'])
        else:
            return response
    raise ValueError('Too many redirects')


def collect_cards(queue_path, output, delay=4, limit=47):
    queue = json.loads(queue_path.read_text())
    output.mkdir(parents=True, exist_ok=True)
    raw = output / 'raw'
    raw.mkdir(exist_ok=True)
    attempted = 0
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        get(client, 'https://kad.arbitr.ru/Version/Change?mode=Full&returnUrl=%2F')
        for entry in queue:
            if entry['status'] != 'pending' or attempted >= limit:
                continue
            attempted += 1
            time.sleep(delay)
            try:
                response = get(client, entry['card_url'])
                raw_path = raw / (entry['case_id'] + '.html')
                raw_path.write_bytes(response.content)
                entry.update(http_status=response.status_code, raw_file=str(raw_path.resolve()),
                             raw_sha256=hashlib.sha256(response.content).hexdigest())
                if response.status_code in (401, 403, 429, 451, 503) or challenge(response.text):
                    entry.update(status='access_blocked', detail='Challenge or access restriction; remaining cases are pending')
                    break
                response.raise_for_status()
                entry['pdf_urls'] = discover_pdfs(response.text, entry['card_url'])
                if not entry['pdf_urls']:
                    entry.update(status='document_links_unavailable', detail='No explicit PDF links in retrieved HTML; no claim that the case has no decisions')
                    break
                for pdf_url in entry['pdf_urls']:
                    time.sleep(delay)
                    pdf = get(client, pdf_url)
                    pdf.raise_for_status()
                    record = extract_pdf(pdf.content, entry['case_id'], pdf_url, output / 'documents')
                    entry.setdefault('documents', []).append({'file': record['file'], 'sha256': record['sha256'], 'text_status': record['text_status']})
                entry['status'] = 'collected_visible_pdf_links'
            except (httpx.HTTPError, ValueError, OSError) as error:
                entry.update(status='failed', detail=str(error))
                break
    queue_path.write_text(json.dumps(queue, ensure_ascii=False, indent=2) + '\n')
    summary = dict(collections.Counter(r['status'] for r in queue))
    (output / 'kad_document_coverage.json').write_text(json.dumps(summary, indent=2) + '\n')
    return summary


def search_targets(queue_path, output, limit=5):
    plan = json.loads(queue_path.read_text())
    raw = output / 'raw'
    raw.mkdir(parents=True, exist_ok=True)
    attempted = 0
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        get(client, 'https://kad.arbitr.ru/Version/Change?mode=Full&returnUrl=%2F')
        for entry in plan['targets']:
            if entry['status'] != 'pending' or attempted >= limit:
                continue
            attempted += 1
            time.sleep(4)
            try:
                response = client.post('https://kad.arbitr.ru/Kad/SearchInstances', json={
                    'Page': 1, 'Count': 25,
                    'Sides': [{'Name': entry['inn'], 'Type': -1, 'ExactMatch': False}],
                    'Judges': [], 'Courts': [], 'CaseNumbers': [],
                    'DateFrom': plan['period_from'] + 'T00:00:00',
                    'DateTo': plan['period_to'] + 'T23:59:59', 'WithVKSInstances': False},
                    headers={'x-date-format': 'iso', 'Referer': 'https://kad.arbitr.ru/'})
                path = raw / ('kad_search_' + entry['inn'] + '.html')
                path.write_bytes(response.content)
                entry.update(http_status=response.status_code, raw_file=str(path.resolve()),
                             raw_sha256=hashlib.sha256(response.content).hexdigest())
                if response.status_code in (401, 403, 429, 451, 503) or challenge(response.text):
                    entry.update(status='access_blocked', detail='Search restriction; remaining targets are pending')
                    break
                response.raise_for_status()
                result = KadParser().parse(response.text, 'html', 'https://kad.arbitr.ru/')
                if result.warnings:
                    entry.update(status='unrecognized_response', detail=result.warnings)
                    break
                case_rows = [json.loads(r.model_dump_json()) for r in result.records]
                for row in case_rows:
                    row['data']['search_participant_inn'] = entry['inn']
                (output / ('kad_search_' + entry['inn'] + '.json')).write_text(json.dumps(case_rows, ensure_ascii=False, indent=2))
                entry.update(status='first_page_collected' if case_rows else 'empty_search_confirmed',
                             case_count=len(case_rows), pages_collected=1)
            except (httpx.HTTPError, ValueError, OSError) as error:
                entry.update(status='failed', detail=str(error))
                break
    queue_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n')
    summary = dict(collections.Counter(r['status'] for r in plan['targets']))
    (output / 'kad_search_coverage.json').write_text(json.dumps(summary, indent=2) + '\n')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('data/compliance_remediation_20261009'))
    parser.add_argument('--collect', action='store_true')
    parser.add_argument('--search', action='store_true')
    parser.add_argument('--limit', type=int, default=47)
    parser.add_argument('--manifest', type=Path, help='Authorized PDF records with case_id, source_url and optional local_file')
    args = parser.parse_args()
    if args.manifest:
        records = []
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            for entry in json.loads(args.manifest.read_text()):
                if entry.get('local_file'):
                    content = Path(entry['local_file']).read_bytes()
                else:
                    time.sleep(4)
                    response = get(client, entry['source_url'])
                    response.raise_for_status()
                    content = response.content
                records.append(extract_pdf(content, entry['case_id'], entry['source_url'], args.output / 'documents'))
        print(json.dumps({'pdf_documents': len(records)}))
        return
    if not (args.output / 'kad_search_queue.json').exists():
        summary = build_queue(Path('data/imports/companies.csv'), [Path('data/extension_20261008/targets.json'), Path('config/russian_defence_entities.json')],
                              Path('data/extension_20261008/exports/kad.jsonl'), args.output)
        print(json.dumps(summary))
    if args.search:
        print(json.dumps(search_targets(args.output / 'kad_search_queue.json', args.output, limit=args.limit)))
    if args.collect:
        print(json.dumps(collect_cards(args.output / 'kad_document_queue.json', args.output, limit=args.limit)))


if __name__ == '__main__':
    main()
