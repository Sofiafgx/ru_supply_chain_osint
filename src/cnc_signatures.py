"""Public hiring/maintenance CNC signatures; one JSON record per mention."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from cnc_trace_search import BRANDS, MODELS, DEFAULT_INNS, Collector as BaseCollector, load, SERVICE, SUPPLY
from parsers.industrial_labor import html_rows, notice_date
from parsers.source_common import soup_html

SITES = {'hh': 'hh.ru', 'trudvsem': 'trudvsem.ru', 'rostender': 'rostender.info',
         'joblab': 'joblab.ru', 'cznelb': 'cznelb.ru'}
CATEGORIES = {
    'cnc_lathe': r'токарн\w*\s+(?:стан\w*\s+)?(?:с\s+)?ЧПУ',
    'five_axis': r'5[\s-]*осев\w*',
    'machining_center': r'обрабатывающ\w*\s+центр',
}
CNC_CONTEXT = re.compile(r'ЧПУ|ЧПК|CNC|станк|фрезер|токар|обрабатывающ\w*\s+центр|5[\s-]*осев', re.I)


def visible_text(value):
    """Keep original text, punctuation and internal whitespace after HTML decoding."""
    soup = BeautifulSoup(str(value or ''), 'html.parser')
    for node in soup.select('script,style,nav,footer,aside'):
        node.decompose()
    for node in soup.select('br'):
        node.replace_with('\n')
    for node in soup.select('p,li,div,h1,h2,h3,tr'):
        node.insert_before('\n')
        node.insert_after('\n')
    return soup.get_text('', strip=False).strip()


def extract_signatures(text):
    signatures = []
    # Paragraphs/list items without a full stop remain evidence fragments.
    # Semicolons are retained as part of the original sentence.
    for sentence in re.split(r'(?<=[.!?])\s+|[\r\n]+', text):
        sentence = sentence.strip()
        if not sentence:
            continue
        categories = [name for name, pattern in CATEGORIES.items() if re.search(pattern, sentence, re.I)]
        found = []
        for brand, alias in BRANDS.items():
            for match in re.finditer(r'(?<!\w)(?:' + alias + r')(?!\w)', sentence, re.I):
                model = re.match(r'[\s:–—-]*(' + MODELS[brand] + r')(?!\w)', sentence[match.end():], re.I)
                found.append({'cnc_brand': brand, 'cnc_model': model[1] if model else None,
                              'text_fragment': sentence, 'equipment_categories': categories})
        if not found and categories:
            found.append({'cnc_brand': None, 'cnc_model': None, 'text_fragment': sentence,
                          'equipment_categories': categories})
        signatures.extend(found)
    return list({(m['cnc_brand'], m['cnc_model'], m['text_fragment']): m for m in signatures}.values())


def classify(mention, rules):
    """Only one unambiguous sourced model rule can produce True."""
    if not mention['cnc_brand'] or not mention['cnc_model']:
        return None, None
    candidates = []
    for rule in rules:
        if rule['brand'] != mention['cnc_brand']:
            continue
        if not re.fullmatch(rule['model_regex'], mention['cnc_model'], re.I):
            continue
        category = rule.get('equipment_category')
        if category and category not in mention['equipment_categories']:
            continue
        candidates.append(rule)
    # Multiple rules conservatively require review, even across jurisdictions.
    if len(candidates) != 1 or candidates[0].get('ambiguous', False):
        return None, None
    rule = candidates[0]
    if rule.get('is_sanctioned_equipment') is not True:
        return None, None
    return True, rule


def validate_rules(rules):
    if not isinstance(rules, list):
        raise ValueError('Rules must be a JSON list')
    ids = set()
    for rule in rules:
        if not isinstance(rule, dict) or not rule.get('rule_id') or rule['rule_id'] in ids:
            raise ValueError('Each rule needs a unique rule_id')
        ids.add(rule['rule_id'])
        if rule.get('brand') not in BRANDS or not isinstance(rule.get('model_regex'), str) or not rule['model_regex']:
            raise ValueError('Each rule needs known brand and nonempty model_regex')
        re.compile(rule['model_regex'], re.I)
        if rule.get('equipment_category') not in (None, *CATEGORIES):
            raise ValueError('Unknown equipment_category')
        if rule.get('jurisdiction') not in ('EU', 'US') or not notice_date(rule.get('checked_at')):
            raise ValueError('Each rule needs EU/US jurisdiction and checked_at date')
        if urlparse(rule.get('basis_url', '')).scheme not in ('http', 'https'):
            raise ValueError('Each rule needs basis_url')
        if type(rule.get('is_sanctioned_equipment')) is not bool or type(rule.get('ambiguous', False)) is not bool:
            raise ValueError('Rule status/ambiguity must be booleans')


def records_from_notice(row, source, targets, rules):
    company_inn = str(row.get('company_inn') or row.get('company_tax_id') or '')
    if company_inn not in targets:
        return [], 'unverified_or_non_target_inn'
    url = row.get('source_url') or ''
    host = urlparse(url).hostname or ''
    if urlparse(url).scheme not in ('http', 'https') or not (host == SITES[source] or host.endswith('.' + SITES[source])):
        return [], 'invalid_source_url'
    title, body = visible_text(row.get('title')), visible_text(row.get('description'))
    text = title + '\n' + body
    if source == 'rostender' and (not SERVICE.search(text) or SUPPLY.search(title)):
        return [], 'not_maintenance_tender'
    if not CNC_CONTEXT.search(text):
        return [], 'no_cnc_context'
    record_id = row.get('record_id')
    if not record_id:
        match = re.search(r'/vacancy/(\d+)|/(\d{7,})-', url)
        record_id = next((x for x in match.groups() if x), None) if match else None
    record_id = str(record_id or 'url_sha256:' + hashlib.sha256(url.encode()).hexdigest()[:24])
    records = []
    for mention in extract_signatures(text):
        flag, basis = classify(mention, rules)
        records.append({
            'company_inn': company_inn, 'source_site': SITES[source], 'record_id': record_id,
            'record_date': notice_date(row.get('record_date') or row.get('posting_date')),
            'text_fragment': mention['text_fragment'], 'cnc_brand': mention['cnc_brand'],
            'cnc_model': mention['cnc_model'], 'source_url': url,
            'is_sanctioned_equipment': flag,
            # Additional fields make the rule and generic category auditable.
            'equipment_categories': mention['equipment_categories'], 'export_control_basis': basis,
        })
    return records, None if records else 'no_signature'


def parse_card(content, source, url):
    rows = html_rows(content, source, url)
    soup = soup_html(content)
    for node in soup.select('footer,aside,nav'):
        node.decompose()
    h1 = soup.find('h1')
    for row in rows:
        if h1:
            row['title'] = str(h1)
        if source == 'cznelb':
            body = soup.select_one('.vacancy-view__body, .vacancy-view__description, .vacancy-view__text')
            if body:
                row['description'] = str(body)
        elif source == 'rostender':
            body = soup.select_one('[itemprop="description"], .tender-description')
            if body:
                row['description'] = str(body)
        elif source == 'joblab':
            sections = []
            for tr in soup.select('tr'):
                cells = tr.find_all(['td', 'th'], recursive=False)
                if len(cells) >= 2 and cells[0].get_text(' ', strip=True).rstrip(' *:') in ('Обязанности', 'Требования', 'Условия'):
                    sections.append(str(cells[1]))
            if sections:
                row['description'] = '\n'.join(sections)
    return rows


class Collector(BaseCollector):
    def fetch(self, url, params=None):
        response = super().fetch(url, params)
        if not hasattr(self, '_pages'):
            self._pages = {}
        if urlparse(url).hostname == 'rostender.info':
            self._pages[url] = response.text
        return response

    def rostender(self, company_inn, seeds):
        rows = super().rostender(company_inn, seeds)
        preserved = []
        for row in rows:
            for original in parse_card(self._pages[row['source_url']], 'rostender', row['source_url']):
                original['company_tax_id'] = company_inn
                preserved.append(original)
        return preserved

    def public_cards(self, source, company_inn, seeds):
        domain = SITES[source]
        urls = set(seeds)
        if not seeds:
            query = f'site:{domain} "{company_inn}" (ЧПУ OR станок OR "обрабатывающий центр")'
            response = self.fetch('https://www.bing.com/search', {'q': query, 'format': 'rss'})
            root = ET.fromstring(response.text)
            urls.update(n.text for n in root.findall('.//item/link') if n.text)
        rows = []
        for url in sorted(urls):
            host = urlparse(url).hostname or ''
            if host != domain and not host.endswith('.' + domain):
                continue
            if not re.search(r'/vacancy/', urlparse(url).path):
                continue
            try:
                response = self.fetch(url)
                soup = soup_html(response.text)
                for node in soup.select('footer,aside,nav,script,style'):
                    node.decompose()
                inns = set()
                for block in soup.select('[itemprop="hiringOrganization"], .vacancy-company, .employer-info, .company-info, .vacancy-view__subtitle'):
                    inns.update(re.findall(r'\bИНН\s*:?\s*(\d{10}|\d{12})(?!\d)', block.get_text(' ', strip=True), re.I))
                if inns != {company_inn}:
                    self.report.append({'source': source, 'url': url, 'status': 'employer_inn_not_verified'})
                    continue
                parsed_rows = parse_card(response.text, source, url)
                if not parsed_rows:
                    self.report.append({'source': source, 'url': url, 'status': 'unsupported_card_layout'})
                for row in parsed_rows:
                    row.update(company_tax_id=company_inn, source_url=url)
                    rows.append(row)
            except (httpx.HTTPError, ValueError) as exc:
                self.report.append({'source': source, 'url': url, 'status': 'error', 'error_type': type(exc).__name__})
        self.report.append({'source': source, 'company_inn': company_inn,
                            'status': 'partial_index_or_seed_discovery', 'candidate_urls': len(urls)})
        return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--inn', nargs='+', default=DEFAULT_INNS)
    p.add_argument('--sources', nargs='+', choices=list(SITES), default=list(SITES))
    p.add_argument('--output', default='data/cnc_signatures/records.json')
    p.add_argument('--offline', help='Normalized JSON notice array; no network calls')
    p.add_argument('--hh-employers', help='JSON: INN -> [{employer_id, evidence_url}]')
    p.add_argument('--seeds', help='JSON: source -> INN -> [individual card URLs]')
    p.add_argument('--export-control', help='JSON list of sourced model rules')
    p.add_argument('--max-pages', type=int, default=10)
    p.add_argument('--delay', type=float, default=1)
    p.add_argument('--timeout', type=float, default=25)
    args = p.parse_args()
    if args.max_pages < 1 or args.delay < 0 or args.timeout <= 0 or any(not re.fullmatch(r'\d{10}|\d{12}', i) for i in args.inn):
        p.error('Invalid INN or request limits')
    rules, mappings, seeds = load(args.export_control, []), load(args.hh_employers, {}), load(args.seeds, {})
    try:
        validate_rules(rules)
        for entries in mappings.values():
            for entry in entries:
                if not re.fullmatch(r'\d+', str(entry.get('employer_id', ''))) or urlparse(entry.get('evidence_url', '')).scheme not in ('http', 'https'):
                    raise ValueError('HH mapping requires numeric employer_id and evidence_url')
    except (ValueError, re.error) as exc:
        p.error(str(exc))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    rows, report = [], []
    if args.offline:
        rows = load(args.offline, [])
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            p.error('Offline input must be an array of notice objects')
    else:
        with httpx.Client(timeout=args.timeout, follow_redirects=True,
                          headers={'User-Agent': 'CNCSignatureResearch/1.0'}) as client:
            collector = Collector(client, output.parent / (output.stem + '_raw'), args.delay, args.max_pages)
            for company_inn in args.inn:
                for source in args.sources:
                    try:
                        source_seeds = seeds.get(source, {}).get(company_inn, [])
                        if source == 'trudvsem':
                            found = collector.trudvsem(company_inn)
                        elif source == 'hh':
                            found = collector.hh(company_inn, mappings.get(company_inn, []))
                        elif source == 'rostender':
                            found = collector.rostender(company_inn, source_seeds)
                        else:
                            found = collector.public_cards(source, company_inn, source_seeds)
                        rows.extend(dict(row, source_site=source) for row in found)
                        report.append({'source': source, 'company_inn': company_inn, 'status': 'request_completed', 'examined': len(found)})
                    except (httpx.HTTPError, ValueError, ET.ParseError) as exc:
                        report.append({'source': source, 'company_inn': company_inn, 'status': 'error',
                                       'error_type': type(exc).__name__})
            report.extend(collector.report)
    records, rejected, seen = [], {}, set()
    for row in rows:
        source = next((k for k, domain in SITES.items() if row.get('source_site') in (k, domain)), None)
        if source not in args.sources:
            rejected['unknown_or_excluded_source'] = rejected.get('unknown_or_excluded_source', 0) + 1
            continue
        found, reason = records_from_notice(row, source, args.inn, rules)
        if reason:
            rejected[reason] = rejected.get(reason, 0) + 1
        for record in found:
            identity = tuple(record[k] for k in ('company_inn', 'source_url', 'text_fragment', 'cnc_brand', 'cnc_model'))
            if identity not in seen:
                records.append(record)
                seen.add(identity)
    output.write_text(json.dumps(records, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    output.with_suffix('.report.json').write_text(json.dumps({
        'timestamp': timestamp, 'records': len(records), 'notices_examined': len(rows),
        'rejected': rejected, 'sources': report, 'hh_mapping_evidence': mappings,
        'coverage': 'Partial public-source discovery; no exhaustive inventory or legal conclusion',
    }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(output.resolve()), 'records': len(records)}, ensure_ascii=False))
    return 2 if any(r['status'] == 'error' for r in report) else 0


if __name__ == '__main__':
    raise SystemExit(main())
