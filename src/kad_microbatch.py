"""Small compliance queue using existing identifiers, with one bounded access attempt."""
import argparse
import collections
import csv
import hashlib
import importlib.metadata
import json
import random
import time
from pathlib import Path

from curl_cffi import requests

from kad_documents import challenge, discover_pdfs, extract_pdf, official_url
from parsers.registry_sources import KadParser
from supplier_enrichment import valid_inn


NAMED = [
    ('1646019914', 'АО «ОЭЗ ППТ «Алабуга»'),
    ('1828020110', 'АО «Воткинский завод»'),
    ('5099000013', 'АО «Корпорация «Тактическое ракетное вооружение»'),
    ('2130094170', 'АО «ВНИИР-Прогресс»'),
    ('6612011546', 'АО «УПКБ «Деталь»'),
]
REJECTED_REPLACEMENTS = ['5018077598', '2128700223', '6612001460']


def build_batch(path=Path('database/employers_2026-10-09.csv')):
    with path.open(encoding='utf-8-sig', newline='') as handle:
        employers = list(csv.DictReader(handle))
    result = [{'inn': i, 'names': [name], 'selection': 'user_named_company_existing_project_inn',
               'identifier_verification': 'checksum_valid_local_reference_not_live_registry_verified',
               'status': 'pending'} for i, name in NAMED]
    used = {r['inn'] for r in result}
    candidates = [r for r in employers if r.get('match_method') == 'inn'
                  and valid_inn(r.get('inn')) and len(r['inn']) == 10
                  and int(r.get('sanctions_count') or 0) > 0]
    candidates.sort(key=lambda r: (-int(r.get('sanctions_count') or 0), r['inn']))
    for row in candidates:
        if row['inn'] in used:
            continue
        used.add(row['inn'])
        result.append({'inn': row['inn'], 'names': [row['name']],
                       'selection': 'local_sanctions_count_descending_exact_inn_match',
                       'local_sanctions_count': int(row['sanctions_count']),
                       'source_file': str(path.resolve()), 'profile_url': row.get('profile_url'),
                       'identifier_verification': 'checksum_valid_local_reference_not_live_registry_verified',
                       'status': 'pending'})
        if len(result) == 20:
            break
    return {'period_from': '2022-01-01', 'period_to': '2026-10-08', 'targets': result,
            'rejected_replacements': [{'inn': i, 'reason': 'invalid_checksum'} for i in REJECTED_REPLACEMENTS],
            'selection_note': 'Compliance workload order based on local data; not a production-importance ranking. Branch aliases sharing an INN are one legal-person search.'}


def archive_response(response, path):
    path.write_bytes(response.content)
    return {'http_status': response.status_code, 'source_url': str(response.url),
            'file': str(path.resolve()), 'sha256': hashlib.sha256(response.content).hexdigest()}


def run(output):
    output.mkdir(parents=True, exist_ok=True)
    raw = output/'raw'
    raw.mkdir(exist_ok=True)
    plan_path = output/'microbatch.json'
    plan = json.loads(plan_path.read_text()) if plan_path.exists() else build_batch()
    with requests.Session(impersonate='chrome', timeout=20) as client:
        # Full mode preserves the site's ordinary cookie flow. No CAPTCHA is solved.
        try:
            response = client.get('https://kad.arbitr.ru/Version/Change?mode=Full&returnUrl=%2F')
            startup = archive_response(response, raw/'kad_full_mode.html')
        except Exception as error:
            startup = {'error': str(error)}
        for target in plan['targets']:
            if target['status'] != 'pending':
                continue
            time.sleep(random.uniform(5, 10))
            try:
                response = client.post('https://kad.arbitr.ru/Kad/SearchInstances',
                    json={'Page': 1, 'Count': 25, 'Sides': [{'Name': target['inn'], 'Type': -1, 'ExactMatch': False}],
                          'Judges': [], 'Courts': [], 'CaseNumbers': [],
                          'DateFrom': plan['period_from']+'T00:00:00',
                          'DateTo': plan['period_to']+'T23:59:59', 'WithVKSInstances': False},
                    headers={'x-date-format': 'iso', 'Referer': 'https://kad.arbitr.ru/'})
                target['search_evidence'] = archive_response(response, raw/('search_'+target['inn']+'.html'))
                if response.status_code in (401, 403, 429, 451, 503) or challenge(response.text):
                    target['status'] = 'access_blocked'
                    break
                response.raise_for_status()
                parsed = KadParser().parse(response.text, 'html', str(response.url))
                if parsed.warnings:
                    target.update(status='unrecognized_response', warnings=parsed.warnings)
                    break
                target['cases'] = [json.loads(r.model_dump_json()) for r in parsed.records]
                target['status'] = 'first_search_page_collected' if parsed.records else 'empty_search_confirmed'
                for case in target['cases']:
                    time.sleep(random.uniform(5, 10))
                    card = client.get(official_url(case['source_url']))
                    case['card_evidence'] = archive_response(card, raw/('card_'+case['record_id']+'.html'))
                    if card.status_code in (401, 403, 429, 451, 503) or challenge(card.text):
                        target['status'] = 'document_access_blocked'
                        break
                    card.raise_for_status()
                    urls = discover_pdfs(card.text, case['source_url'])
                    case['document_status'] = 'visible_pdf_links_found' if urls else 'document_links_unavailable'
                    for url in urls:
                        time.sleep(random.uniform(5, 10))
                        pdf = client.get(official_url(url), allow_redirects=False)
                        pdf.raise_for_status()
                        if 300 <= pdf.status_code < 400:
                            raise ValueError('PDF redirect requires verified destination')
                        record = extract_pdf(pdf.content, case['record_id'], url, output/'documents')
                        case.setdefault('documents', []).append({'file': record['file'], 'sha256': record['sha256']})
                if target['status'] == 'document_access_blocked':
                    break
            except Exception as error:
                target.update(status='failed', error=str(error))
                break
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2)+'\n')
    summary = {'transport': 'curl_cffi_chrome', 'curl_cffi_version': importlib.metadata.version('curl_cffi'),
               'startup': startup, 'targets': len(plan['targets']),
               'states': dict(collections.Counter(r['status'] for r in plan['targets'])),
               'pdf_documents': sum(len(c.get('documents', [])) for t in plan['targets'] for c in t.get('cases', [])),
               'note': 'Stops on shared access restriction; unattempted targets remain pending. Only first search pages and visible PDF links are supported.'}
    (output/'microbatch_coverage.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('data/compliance_text_20261009'))
    args = parser.parse_args()
    print(json.dumps(run(args.output),ensure_ascii=False))


if __name__ == '__main__':
    main()
