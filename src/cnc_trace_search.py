"""Search public CNC traces; keep company attribution and screening auditable."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from parsers.industrial_labor import BRANDS as BASE_BRANDS, MODELS as BASE_MODELS
from parsers.industrial_labor import html_rows, json_rows, notice_date
from parsers.source_common import soup_html

DEFAULT_INNS = ['2128700223', '1804002498', '6612001460', '6444009038']
BRANDS = dict(BASE_BRANDS, Heidenhain=r'Heidenhain|Хайденхайн|Хейденхайн',
              Mazak=r'Mazak|Мазак', Hermle=r'Hermle|Хермле', Studer=r'Studer|Штудер|Студер')
MODELS = dict(BASE_MODELS,
    Heidenhain=r'(?:iTNC|TNC)[\s-]*\d{3,4}',
    Mazak=r'(?:INTEGREX|VARIAXIS|QUICK\s+TURN|QTN|QT|VCN|VTC|HQR)[\s-]*(?:[a-z]-?)?\d+[a-z0-9/-]*',
    Hermle=r'C[\s-]*\d{1,3}(?:\s+U)?',
    Studer=r'S[\s-]*\d{2,3}')
# Generic keyword hits are retained even if no brand/model can be established.
GENERIC = re.compile(r'токарн\w*\s+(?:стан\w*\s+)?(?:с\s+)?ЧПУ|5[\s-]*осев\w*|фрезерн\w*\s+обрабатывающ\w*\s+центр', re.I)
CNC = re.compile(r'ЧПУ|CNC|станк|обрабатывающ\w*\s+центр|фрезер|токар|5[\s-]*осев', re.I)
ROLE = re.compile(r'оператор|наладчик|наладчиц|наладк', re.I)
SERVICE = re.compile(r'ремонт|сервис|техническ\w*\s+обслуж|\bТО\b|диагност', re.I)
SUPPLY = re.compile(r'^\s*(?:тендер\s*:\s*)?(?:поставка|закупка|приобретение)\b', re.I)
SITES = {'hh': 'hh.ru', 'trudvsem': 'trudvsem.ru', 'rostender': 'rostender.info'}


def plain(value):
    s = BeautifulSoup(str(value or ''), 'html.parser')
    for x in s.select('script,style,nav,footer,aside'):
        x.decompose()
    return s.get_text(' ', strip=True)


def extract(text):
    """Sentence-level context; model must immediately follow its brand."""
    result = []
    sentences = re.split(r'(?<=[.!?;])\s+|[\r\n]+', text)
    for sentence in sentences:
        if not sentence.strip():
            continue
        for brand, alias in BRANDS.items():
            for match in re.finditer(r'(?<!\w)(?:' + alias + r')(?!\w)', sentence, re.I):
                m = re.match(r'[\s:–—-]*(' + MODELS[brand] + r')(?!\w)', sentence[match.end():], re.I)
                result.append({'cnc_brand': brand, 'cnc_model': m[1].strip() if m else None,
                               'text_fragment': sentence.strip()})
        if GENERIC.search(sentence) and not any(x['text_fragment'] == sentence.strip() for x in result):
            result.append({'cnc_brand': None, 'cnc_model': None, 'text_fragment': sentence.strip()})
    return list({(x['cnc_brand'], x['cnc_model'], x['text_fragment']): x for x in result}.values())


def screening(mention, rules):
    """True means match to the supplied, sourced rule; unknown is never False."""
    for rule in rules:
        if mention['cnc_brand'] != rule['brand']:
            continue
        if re.search(rule.get('model_regex', '.*'), mention['cnc_model'] or '', re.I):
            return {'is_sanctioned_equipment': True, 'sanctions_basis': rule,
                    'sanctions_status': 'configured_rule_match_requires_legal_review'}
    return {'is_sanctioned_equipment': None, 'sanctions_basis': None,
            'sanctions_status': 'not_established'}


def normalize(row, source, targets, rules, timestamp):
    company_inn = str(row.get('company_tax_id') or '')
    if company_inn not in targets:
        return None, 'unverified_or_non_target_inn'
    url = row.get('source_url') or ''
    host = urlparse(url).hostname or ''
    if urlparse(url).scheme not in ('http', 'https') or not (host == SITES[source] or host.endswith('.' + SITES[source])):
        return None, 'not_a_source_evidence_url'
    title, body = plain(row.get('title')), plain(row.get('description'))
    content = title + '\n' + body
    if source != 'rostender' and not ROLE.search(title):
        return None, 'not_operator_or_setter'
    if source == 'rostender' and (not SERVICE.search(content) or SUPPLY.search(title)):
        return None, 'not_maintenance_tender'
    if not CNC.search(content):
        return None, 'no_cnc_context'
    mentions = extract(content)
    if not mentions:
        return None, 'no_keyword'
    rid = row.get('record_id')
    if not rid:
        match = re.search(r'/vacancy/(\d+)|/(\d{7,})-', url)
        rid = next((v for v in match.groups() if v), None) if match else None
    # A URL hash is explicitly labelled, never claimed to be the site's ID.
    rid = str(rid or 'url_sha256:' + hashlib.sha256(url.encode()).hexdigest()[:24])
    detected = []
    for mention in mentions:
        detected.append(dict(mention, company_inn=company_inn, source_site=SITES[source],
                             record_id=rid, record_date=notice_date(row.get('posting_date')),
                             **screening(mention, rules)))
    return {'company_inn': company_inn, 'detected_cnc_list': detected,
            'evidence_url': url, 'timestamp': timestamp}, None


class Collector:
    def __init__(self, client, raw_dir, delay, max_pages):
        self.client, self.raw_dir, self.delay, self.max_pages = client, raw_dir, delay, max_pages
        self.report = []

    def fetch(self, url, params=None):
        time.sleep(self.delay)
        r = self.client.get(url, params=params)
        r.raise_for_status()
        key = hashlib.sha256(str(r.url).encode()).hexdigest()
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        (self.raw_dir / (key + '.txt')).write_text(r.text, encoding='utf-8')
        return r

    def trudvsem(self, company_inn):
        rows = []
        for page in range(self.max_pages):
            payload = self.fetch(f'https://opendata.trudvsem.ru/api/v1/vacancies/company/inn/{company_inn}',
                                 {'offset': page * 100, 'limit': 100}).json()
            status = payload.get('status')
            if status not in (None, '200', 200):
                raise ValueError(f'Trudvsem API status: {status}')
            items = (payload.get('results') or {}).get('vacancies', [])
            if not isinstance(items, list):
                raise ValueError('Unexpected Trudvsem response')
            for item in items:
                v = item.get('vacancy', item)
                for row in json_rows(v, 'trudvsem'):
                    # Official INN-scoped endpoint is attribution evidence.
                    if row.get('company_tax_id') and str(row['company_tax_id']) != company_inn:
                        raise ValueError('Trudvsem company INN mismatch')
                    row.update(company_tax_id=company_inn, record_id=v.get('id'))
                    rows.append(row)
            if len(items) < 100:
                return rows
        self.report.append({'source': 'trudvsem', 'company_inn': company_inn, 'status': 'page_limit_partial'})
        return rows

    def hh(self, company_inn, mappings):
        rows = []
        if not mappings:
            raise ValueError('HH requires confirmed employer IDs in --hh-employers')
        for mapping in mappings:
            eid = mapping['employer_id']
            for page in range(min(self.max_pages, 20)):
                data = self.fetch('https://api.hh.ru/vacancies',
                                  {'employer_id': eid, 'per_page': 100, 'page': page}).json()
                if not isinstance(data.get('items'), list):
                    raise ValueError('Unexpected HH response')
                for item in data['items']:
                    v = self.fetch(f"https://api.hh.ru/vacancies/{item['id']}").json()
                    if str((v.get('employer') or {}).get('id')) != str(eid):
                        raise ValueError('HH employer mismatch')
                    for row in json_rows(v, 'hh'):
                        row.update(company_tax_id=company_inn, record_id=v['id'])
                        rows.append(row)
                if page + 1 >= data.get('pages', 0):
                    break
            else:
                self.report.append({'source': 'hh', 'company_inn': company_inn, 'status': 'page_limit_partial'})
        return rows

    def rostender(self, company_inn, seeds):
        # Search-index discovery is partial; only individual cards become evidence.
        urls = set(seeds)
        if not seeds:
            query = f'site:rostender.info "{company_inn}" (ремонт OR обслуживание OR сервис)'
            response = self.fetch('https://www.bing.com/search', {'q': query, 'format': 'rss'})
            root = ET.fromstring(response.text)
            urls.update(x.text for x in root.findall('.//item/link') if x.text)
        rows = []
        for url in sorted(urls):
            parsed = urlparse(url)
            if parsed.hostname != 'rostender.info' or not re.search(r'/\d{7,}-', parsed.path):
                continue
            try:
                response = self.fetch(url)
                soup = soup_html(response.text)
                # Never attribute a footer/portal INN or another recommended tender.
                customer_inns = set()
                for block in soup.select('[itemprop="buyer"], [itemprop="customer"], .tender-customer, .customer-info, .tender-info__customer'):
                    customer_inns.update(re.findall(r'\bИНН\s*:?\s*(\d{10}|\d{12})(?!\d)', block.get_text(' ', strip=True), re.I))
                if company_inn not in customer_inns:
                    self.report.append({'source': 'rostender', 'url': url, 'status': 'customer_inn_not_verified'})
                    continue
                for row in html_rows(response.text, 'rostender', url):
                    row['company_tax_id'] = company_inn
                    rows.append(row)
            except (httpx.HTTPError, ValueError) as exc:
                self.report.append({'source': 'rostender', 'url': url, 'status': 'error', 'error_type': type(exc).__name__})
        self.report.append({'source': 'rostender', 'company_inn': company_inn,
                            'status': 'partial_index_or_seed_discovery', 'candidate_urls': len(urls)})
        return rows


def load(path, default):
    return json.loads(Path(path).read_text(encoding='utf-8')) if path else default


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--inn', nargs='+', default=DEFAULT_INNS)
    p.add_argument('--output', default='data/cnc_traces/records.json')
    p.add_argument('--hh-employers', help='JSON: INN -> [{employer_id, evidence_url}]')
    p.add_argument('--rostender-seeds', help='JSON: INN -> [individual tender URLs]')
    p.add_argument('--sanctions-rules', help='JSON list: brand, model_regex, jurisdiction, basis_url, checked_at')
    p.add_argument('--offline', help='Normalized JSON rows with source_site, company_tax_id, title, description, source_url')
    p.add_argument('--sources', nargs='+', choices=list(SITES), default=list(SITES))
    p.add_argument('--max-pages', type=int, default=10)
    p.add_argument('--delay', type=float, default=1.0)
    p.add_argument('--timeout', type=float, default=25.0)
    args = p.parse_args()
    if args.max_pages < 1 or args.delay < 0 or args.timeout <= 0 or any(not re.fullmatch(r'\d{10}|\d{12}', i) for i in args.inn):
        p.error('Invalid INN, delay, timeout or page limit')
    mappings, seeds, rules = load(args.hh_employers, {}), load(args.rostender_seeds, {}), load(args.sanctions_rules, [])
    for entries in mappings.values():
        for entry in entries:
            if not re.fullmatch(r'\d+', str(entry.get('employer_id', ''))) or not re.match(r'https?://', entry.get('evidence_url', '')):
                p.error('Each HH mapping requires employer_id and evidence_url confirming the INN')
    for rule in rules:
        if rule.get('brand') not in BRANDS or rule.get('jurisdiction') not in ('EU', 'US') or not re.match(r'https?://', rule.get('basis_url', '')) or not notice_date(rule.get('checked_at')):
            p.error('Sanctions rule requires known brand, EU/US jurisdiction, basis_url, checked_at')
        re.compile(rule.get('model_regex', '.*'))
    timestamp = datetime.now(timezone.utc).isoformat()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    report, rows = [], []
    if args.offline:
        rows = load(args.offline, [])
    else:
        with httpx.Client(timeout=args.timeout, follow_redirects=True,
                          headers={'User-Agent': 'CNCTraceResearch/1.0'}) as client:
            collector = Collector(client, output.parent / (output.stem + '_raw'), args.delay, args.max_pages)
            for company_inn in args.inn:
                for source in args.sources:
                    try:
                        found = (collector.trudvsem(company_inn) if source == 'trudvsem' else
                                 collector.hh(company_inn, mappings.get(company_inn, [])) if source == 'hh' else
                                 collector.rostender(company_inn, seeds.get(company_inn, [])))
                        rows.extend(dict(r, source_site=source) for r in found)
                        report.append({'source': source, 'company_inn': company_inn, 'status': 'request_completed', 'examined': len(found)})
                    except (httpx.HTTPError, ValueError, ET.ParseError) as exc:
                        report.append({'source': source, 'company_inn': company_inn, 'status': 'error',
                                       'error_type': type(exc).__name__, 'reason': str(exc) if isinstance(exc, ValueError) else None})
            report.extend(collector.report)
    records, seen, rejected = [], set(), {}
    for row in rows:
        source = row.get('source_site')
        source = next((k for k, v in SITES.items() if source in (k, v)), None)
        if source not in args.sources:
            continue
        record, reason = normalize(row, source, args.inn, rules, timestamp)
        if record:
            key = (record['company_inn'], record['evidence_url'])
            if key not in seen:
                records.append(record)
                seen.add(key)
        else:
            rejected[reason] = rejected.get(reason, 0) + 1
    output.write_text(json.dumps(records, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    output.with_suffix('.report.json').write_text(json.dumps({'timestamp': timestamp, 'records': len(records),
        'rejected': rejected, 'sources': report, 'hh_mapping_evidence': mappings,
        'coverage': 'Public current vacancies and indexed/seed tenders; not exhaustive historical inventory'}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(output.resolve()), 'records': len(records), 'report': str(output.with_suffix('.report.json').resolve())}, ensure_ascii=False))
    return 2 if any(x['status'] == 'error' for x in report) else 0


if __name__ == '__main__':
    raise SystemExit(main())
