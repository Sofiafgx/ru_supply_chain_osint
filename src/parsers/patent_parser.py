"""Evidence-first patent parsing. No inferred chip consumption from channel counts alone."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from parsers.source_common import date, soup_html

TARGETS = {
    'АО "ВНИИР-Прогресс"': ['ВНИИР-Прогресс', 'ВНИИР Прогресс', 'VNIIR Progress', 'VNIIR-Progress'],
    'АО "НИЦЭВТ"': ['НИЦЭВТ', 'NITsEVT', 'NICEVT', 'Научно-исследовательский центр электронной вычислительной техники'],
    'АО "НИИ КП"': ['НИИ КП', 'НИИ космического приборостроения', 'Научно-исследовательский институт космического приборостроения', 'NII KP', 'Research Institute of Space Instrumentation'],
    'АО АНПП "Темп-Авиа"': ['Темп-Авиа', 'Темп Авиа', 'Temp-Avia', 'Temp Avia'],
}
IPC_PREFIXES = ('H01Q', 'G01S', 'G06F')
TERMS = {
    'ADC': r'\bАЦП\b|аналог[оа][\s‐‑–-]*цифров\w*\s+преобразовател\w*|\bADC\b|analog.to.digital converter',
    'FPGA': r'\bПЛИС\b|программируем\w*\s+логическ\w*\s+интегральн\w*\s+схем\w*|\bFPGA\b',
    'receive_path': r'тракт\w*\s+при[её]м\w*|при[её]мн\w*\s+тракт\w*|receiv\w*\s+path',
    'clock_generator': r'генератор\w*\s+тактов\w*\s+импульс\w*|clock generator',
    'multichannel': r'многоканальн\w*|multi.channel',
    'channel_count': r'количеств\w*\s+канал\w*|числ[оа]\s+(?:при[её]мн\w*\s+)?канал\w*|number of channels',
    'bandpass_filter': r'полосов\w*\s+фильтр\w*|band.pass filter',
}
KEYWORDS = re.compile('|'.join(f'(?:{v})' for k, v in TERMS.items() if k != 'bandpass_filter'), re.I)
COMPONENTS = {k: re.compile(v, re.I) for k, v in TERMS.items() if k in ('ADC', 'FPGA', 'clock_generator', 'bandpass_filter')}
PER_CHANNEL = re.compile(r'(?:кажд\w*|каждому)\s+(?:при[её]мн\w*\s+|из\s+\w+\s+)?канал\w*\s+(?:содерж\w*|включ\w*|имеет)|(?:в|на)\s+кажд\w*\s+(?:при[её]мн\w*\s+)?канал\w*\s+(?:установлен\w*|содерж\w*|размещ\w*)|each\s+(?:receiv\w*\s+)?channel\s+(?:contains|includes|has)', re.I)
PRIOR_ART = re.compile(r'известн\w*|прототип\w*|аналог\b|уровень техники|prior art|known device|closest analogue', re.I)
SHARED = re.compile(r'общ\w*\s+(?:для|на)|один\s+(?:общий|на\s+все)|совместн\w*|shared|common to', re.I)
NEGATION = re.compile(r'\bне\s+(?:содерж\w*|включ\w*|имеет|установлен\w*)|\bбез\b|does not|without', re.I)
WORDS = {'два': 2, 'двух': 2, 'три': 3, 'трех': 3, 'трёх': 3, 'четыре': 4, 'четырех': 4, 'четырёх': 4,
         'восемь': 8, 'восьми': 8, 'двенадцать': 12, 'двенадцати': 12, 'шестнадцать': 16, 'шестнадцати': 16}
NUM = r'(?:\d+|' + '|'.join(WORDS) + r')'
COUNT_PATTERNS = [
    re.compile(r'(?<!\w)(?:N|M|К|K)\s*=\s*(\d+(?:\s*[,;/]\s*\d+)*)(?!\d)', re.I),
    re.compile(r'(?<!\w)(' + NUM + r')\s+(?:при[её]мн\w*\s+)?канал(?:а|ов)?\b', re.I),
    re.compile(r'(?<!\w)(' + NUM + r')[\s‐‑–-]*канальн\w*', re.I),
    re.compile(r'(?:количеств\w*|число)\s+(?:при[её]мн\w*\s+)?канал\w*\s*(?:составляет|равно|равен|=|:)\s*(' + NUM + r')', re.I),
]


def clean(text):
    return re.sub(r'\s+', ' ', str(text or '')).strip()


def match_target(name):
    value = re.sub(r'[^\w]+', ' ', name.casefold()).strip()
    for canonical, aliases in TARGETS.items():
        for alias in aliases:
            normalized = re.sub(r'[^\w]+', ' ', alias.casefold()).strip()
            if re.search(r'(?<!\w)' + re.escape(normalized) + r'(?!\w)', value):
                return canonical
    return None


def ipc_codes(text):
    return sorted(set(re.sub(r'\s+', '', m).upper() for m in re.findall(r'\b[A-H]\s*\d{2}\s*[A-Z](?:\s*\d+\s*/\s*\d+)?', text, re.I)))


@dataclass
class Patent:
    patent_number: str
    assignees: list[str]
    publication_date: str | None
    device_title: str
    ipc: list[str] = field(default_factory=list)
    cpc: list[str] = field(default_factory=list)
    sections: dict[str, list[str]] = field(default_factory=dict)
    source_url: str = ''
    drawing_urls: list[str] = field(default_factory=list)
    pdf_url: str = ''


def parse_html(content, source_url=''):
    soup = soup_html(content)
    def metas(name, scheme=None):
        return [clean(e.get('content')) for e in soup.find_all('meta', attrs={'name': name}) if scheme is None or e.get('scheme') == scheme]
    def one(name, scheme=None):
        return next(iter(metas(name, scheme)), '')
    number = re.sub(r'[^A-Za-z0-9]', '', one('citation_patent_number'))
    if not number:
        element = soup.select_one('[itemprop="publicationNumber"]')
        number = clean(element.get_text()) if element else ''
    if not number:
        return parse_fips_html(soup, source_url)
    # Original assignee metadata only, never citations, timeline or body mentions.
    assignees = metas('DC.contributor', 'assignee')
    ipc, cpc = [], []
    for node in soup.select('li[itemprop="classifications"]'):
        code = node.select_one('[itemprop="Code"]')
        marker = node.select_one('[itemprop="IsCPC"]')
        if code:
            target = cpc if marker and marker.get('content') == 'true' else ipc
            target.extend(ipc_codes(code.get_text()))
    for node in soup.select('[itemprop="ipcCode"], meta[name="citation_ipc"]'):
        ipc.extend(ipc_codes(node.get('content') or node.get_text()))
    sections = {}
    for section, selector in [('claims', 'section[itemprop="claims"] .claim-text'),
                              ('description', 'section[itemprop="description"] .description-paragraph')]:
        nodes = soup.select(selector)
        if section == 'claims':
            nodes = [n for n in nodes if not n.find_parent(class_='claim-text')]
        if not nodes:
            nodes = soup.select(f'section[itemprop="{section}"] [itemprop="content"]')
        sections[section] = list(dict.fromkeys(clean(n.get_text(' ', strip=True)) for n in nodes if clean(n.get_text())))
    drawings = [a.get('href') for a in soup.select('a[href]') if re.search(r'patentimages.*\.(?:png|jpg|jpeg)$', a.get('href', ''), re.I)]
    published = one('DC.date', 'issue') or one('DC.date', 'datePublished') or one('citation_publication_date')
    if not published:
        node = soup.select_one('time[itemprop="publicationDate"]')
        published = node.get('datetime') or node.get_text() if node else ''
    return Patent(number, assignees, date(published), one('DC.title'), sorted(set(ipc)),
                  sorted(set(cpc)), sections, source_url, sorted(set(drawings)), one('citation_pdf_url'))


def parse_fips_html(soup, source_url=''):
    """INID-labelled FIPS full-text pages. Reject indexes/abstract-only evidence."""
    text = soup.get_text('\n', strip=True)
    def inid(code):
        match = re.search(r'\(' + code + r'\)\s*(.*?)(?=\(\d{2}\)|$)', text, re.S)
        return clean(match[1]) if match else ''
    number_field = inid('11')
    number_match = re.search(r'\b(?:RU\s*)?(\d{5,})(?:\s*([A-Z]\d?))?\b', number_field)
    kind_match = re.search(r'\b([A-Z]\d?)\b', inid('13'))
    if not number_match or not (number_match[2] or kind_match):
        raise ValueError('No supported publication metadata (Google meta / FIPS INID / ST.36)')
    number = 'RU' + number_match[1] + (number_match[2] or kind_match[1])
    publication = re.search(r'\b\d{2}\.\d{2}\.\d{4}\b|\b\d{4}-\d{2}-\d{2}\b', inid('45') or inid('43'))
    owner = re.sub(r'^(?:Патентообладател\w*|Заявител\w*)\s*:?\s*', '', inid('73'), flags=re.I)
    sections = {}
    # Headings are mandatory: never treat an abstract or bulletin index as full text.
    heading = re.compile(r'^\s*(Формула\s+(?:изобретения|полезной модели)|Описание\s+(?:изобретения|полезной модели))(?:\s*[:.]\s*)?$', re.I | re.M)
    matches = list(heading.finditer(text))
    for i, match in enumerate(matches):
        key = 'claims' if match[1].lower().startswith('формула') else 'description'
        body = text[match.end():matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        sections.setdefault(key, []).extend(clean(line) for line in body.splitlines() if clean(line))
    return Patent(number, [owner] if owner else [], date(publication[0]) if publication else None,
                  re.sub(r'^(?:Название|Наименование)\s*:?\s*', '', inid('54'), flags=re.I),
                  ipc_codes(inid('51')), [], sections, source_url)


def parse_xml(content, source_url=''):
    """ST.36 publication XML; strip namespaces without resolving external entities."""
    root = ET.fromstring(content)
    for node in root.iter():
        node.tag = node.tag.split('}')[-1]
    def texts(path):
        return [clean(' '.join(n.itertext())) for n in root.findall(path)]
    def value(path):
        return next(iter(texts(path)), '')
    ref = root.find('.//publication-reference/document-id')
    if ref is None:
        raise ValueError('Missing ST.36 publication-reference')
    get = lambda key: clean(ref.findtext(key))
    number = get('country') + get('doc-number') + get('kind')
    raw_date = get('date')
    if re.fullmatch(r'\d{8}', raw_date):
        raw_date = f'{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}'
    assignees = texts('.//parties/assignees/assignee/addressbook/orgname') or texts('.//parties/applicants/applicant/addressbook/orgname')
    ipc = ipc_codes(' '.join(texts('.//classification-ipc') + texts('.//classification-ipcr')))
    # ST.36 IPCR can split section/class/subclass/main-group/subgroup into elements.
    for node in root.findall('.//classification-ipcr'):
        parts = [clean(node.findtext(k)) for k in ('section', 'class', 'subclass')]
        if all(parts):
            ipc.extend(ipc_codes(''.join(parts)))
    sections = {key: texts('.//' + key + '/p') for key in ('description',)}
    sections['claims'] = texts('.//claims/claim')
    return Patent(number, assignees, date(raw_date), value('.//invention-title'), sorted(set(ipc)), [], sections, source_url)


def from_json(row):
    assignees = row.get('assignees') or [row.get('assignee_name', '')]
    if not isinstance(assignees, list):
        raise ValueError('assignees must be a list')
    sections = {}
    for key in ('claims', 'description'):
        value = row.get(key, row.get('sections', {}).get(key, []))
        sections[key] = [clean(v) for v in (value if isinstance(value, list) else value.splitlines()) if clean(v)]
    number = str(row.get('patent_number', ''))
    if not re.fullmatch(r'[A-Z]{2}\d+[A-Z]\d?', number):
        raise ValueError('patent_number must be a publication identifier including kind code')
    ipc = row.get('ipc', [])
    if not isinstance(ipc, list):
        raise ValueError('ipc must be a list')
    return Patent(number, assignees, date(row.get('publication_date')), clean(row.get('device_title')),
                  ipc_codes(' '.join(ipc)), [], sections, row.get('source_url', ''))


def counts(snippet):
    values, evidence = set(), []
    alternatives = re.compile(r'(?<!\w)(\d+(?:\s*[-–]?\s*(?:,|или|либо|or)\s*\d+)+)\s*[-–]?\s*(?:при[её]мн\w*\s+)?канал\w*', re.I)
    for match in alternatives.finditer(snippet):
        values.update(int(n) for n in re.findall(r'\d+', match[1]) if 0 < int(n) <= 65536)
        evidence.append(match[0])
    for index, pattern in enumerate(COUNT_PATTERNS):
        for match in pattern.finditer(snippet):
            # N=4 is not a channel count unless explicitly tied to channels in this sentence.
            if index == 0:
                sentence_start = max(snippet.rfind('.', 0, match.start()), snippet.rfind(';', 0, match.start())) + 1
                sentence_end = snippet.find('.', match.end())
                sentence = snippet[sentence_start:sentence_end if sentence_end >= 0 else len(snippet)]
                symbol = re.match(r'[NMКK]', match[0], re.I)[0]
                tied = re.search(r'(?:числ[оа]|количеств\w*)\s+(?:при[её]мн\w*\s+)?канал\w*\s*(?:обозначен\w*\s+)?' + re.escape(symbol) + r'\s*=', sentence, re.I)
                symbolic_channels = re.search(r'\b' + re.escape(symbol) + r'\s+(?:при[её]мн\w*\s+)?канал\w*', sentence, re.I)
                if not (tied or symbolic_channels):
                    continue
            raw = match[1].casefold()
            found = [WORDS[raw]] if raw in WORDS else [int(n) for n in re.findall(r'\d+', raw)]
            if any(n < 1 or n > 65536 for n in found):
                continue
            values.update(found)
            evidence.append(match[0])
    return sorted(values), list(dict.fromkeys(evidence))


def analyze(snippet):
    values, evidence = counts(snippet)
    symbolic = bool(re.search(r'\b[NM]\s+(?:при[её]мн\w*\s+)?канал\w*|(?:число|количество)\s+канал\w*\s*[=:]\s*[NM]\b', snippet, re.I))
    reasons = []
    if len(values) > 1:
        reasons.append('multiple_channel_counts')
    if re.search(r'не менее|не более|до\s+\d+|от\s+\d+|\d+\s*[-–]\s*\d+|at least|up to', snippet, re.I):
        reasons.append('bounded_or_range_count')
    if PRIOR_ART.search(snippet):
        reasons.append('prior_art_context')
    if SHARED.search(snippet):
        reasons.append('shared_component_context')
    if NEGATION.search(snippet):
        reasons.append('negated_context')
    if re.search(r'передающ\w*\s+канал|канал\w*\s+передач|transmit\w*\s+channel', snippet, re.I):
        reasons.append('mixed_or_transmit_channel_scope')
    multipliers = {}
    for sentence in re.split(r'(?<=[.!?])\s+', snippet):
        each = PER_CHANNEL.search(sentence)
        if not each:
            continue
        # Only components inside the contains-clause, before a new subject/contrast.
        clause = re.split(r';|\bа\s+|\bно\s+|\bпри этом\b', sentence[each.end():], maxsplit=1, flags=re.I)[0]
        if re.search(r'\bили\b|\bлибо\b|\beither\b|\bor\b', clause, re.I):
            reasons.append('alternative_components')
        for component, pattern in COMPONENTS.items():
            match = pattern.search(clause)
            if not match:
                continue
            prefix = clause[max(0, match.start() - 35):match.start()]
            qty = re.search(r'(' + NUM + r')\s*$', prefix, re.I)
            per_channel = WORDS.get(qty[1].casefold(), int(qty[1]) if qty and qty[1].isdigit() else 1) if qty else 1
            if not 1 <= per_channel <= 65536:
                reasons.append('invalid_component_quantity')
            if re.search(r'по\s+\d+|нескольк\w*|групп\w*|\d+\s*[-–,]\s*\d+', prefix, re.I):
                reasons.append('ambiguous_component_quantity')
            if component in multipliers and multipliers[component]['per_channel'] != per_channel:
                reasons.append('conflicting_component_quantity')
            multipliers[component] = {'per_channel': per_channel, 'r_kj': 'N' if per_channel == 1 else f'{per_channel}*N',
                                      'value': values[0] * per_channel if len(values) == 1 else None,
                                      'component_type': 'structural_element' if component == 'bandpass_filter' else 'functional_component'}
    reasons = sorted(set(reasons))
    if reasons:
        multipliers = {}
    if not values and not symbolic:
        reasons.append('channel_count_not_explicit')
        multipliers = {}
    if not multipliers:
        reasons.append('no_verified_per_channel_component_relation')
    return {'values': values, 'symbol': 'N' if values or symbolic else None, 'evidence': evidence}, multipliers or None, sorted(set(reasons))


def records(patent, prefixes=IPC_PREFIXES):
    targets = [(name, match_target(name)) for name in patent.assignees if match_target(name)]
    if not targets:
        return [], 'non_target_or_missing_assignee'
    if not any(code.startswith(tuple(prefixes)) for code in patent.ipc):
        return [], 'missing_or_non_target_ipc'
    if not patent.publication_date or not patent.device_title:
        return [], 'missing_publication_metadata'
    out = []
    for section, paragraphs in patent.sections.items():
        for i, paragraph in enumerate(paragraphs):
            if not KEYWORDS.search(paragraph):
                continue
            snippet = paragraph
            # Adjacent count-only paragraph may support a per-channel clause. Never borrow
            # a remote count from a different claim or from the complete document.
            if section == 'description' and PER_CHANNEL.search(paragraph) and not counts(paragraph)[0] and i:
                previous = paragraphs[i - 1]
                if len(previous) <= 400 and counts(previous)[0] and not PRIOR_ART.search(previous):
                    snippet = previous + '\n' + paragraph
            channels, multiplier, reasons = analyze(snippet)
            for actual, canonical in targets:
                out.append({'patent_number': patent.patent_number, 'assignee_name': actual,
                            'publication_date': patent.publication_date, 'device_title': patent.device_title,
                            'detected_architecture_channels': channels,
                            'calculated_component_multiplier': multiplier, 'raw_snippet_quote': snippet,
                            'canonical_assignee_name': canonical, 'source_url': patent.source_url,
                            'ipc_codes': patent.ipc, 'section': section, 'paragraph_index': i,
                            'matched_terms': [k for k, pattern in TERMS.items() if re.search(pattern, snippet, re.I)],
                            'review_reasons': reasons, 'drawing_urls': patent.drawing_urls,
                            'interpretation': 'functional_architecture_not_verified_chip_bom'})
    return out, 'accepted' if out else 'no_keyword_fragments'
