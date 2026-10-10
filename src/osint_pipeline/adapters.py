"""Conservative source adapters; explicit column maps support licensed exports."""
from __future__ import annotations

import csv
import io
import json
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from lxml import etree

from parsers.source_common import SourceError, get, inn, number, soup_html, table_rows, text
from parsers.industrial_labor import MAINTENANCE, equipment_mentions, html_rows as notice_html, json_rows as notice_json
from parsers.patent_parser import COMPONENTS, WORDS, analyze, parse_html as patent_html, parse_xml as patent_xml, from_json as patent_json
from osint_pipeline.schema import FIELDS
from osint_pipeline.transport import CollectionError

SOURCES = {
    1: ('zakupki', 'checko', 'clearspending', 'kad'),
    2: ('hh', 'trudvsem', 'rostender', 'joblab', 'cznelb'),
    3: ('arshin',),
    4: ('comtrade', 'panjiva', 'importgenius', '52wmb', 'c4ads'),
    5: ('google_patents', 'fips'),
    6: ('firms', 'copernicus', 'beyondparallel', 'militarnyi', 'lostarmour'),
    7: ('elibrary', 'proverki'),
}
ALIASES = {
    'contract_id': ('regNum', 'regnum', 'contract_number', 'CaseNumber', 'case_number', 'Номер контракта', 'Номер дела'),
    'contract_date': ('signDate', 'sign_date', 'registration_date', 'Date', 'Дата заключения контракта'),
    'subject_text': ('subject', 'title', 'claim_description', 'Предмет контракта'),
    'price_rub': ('amount_rur', 'claim_amount_rub', 'Цена контракта, руб.'),
    'company_inn': ('company_tax_id', 'ИНН организации'),
    'record_date': ('posting_date', 'published_at', 'publication_date', 'Дата публикации'),
    'facility_name': ('miOwner',), 'facility_inn': ('miOwnerInn',),
    'verification_date': ('vrfDate',), 'equipment_type': ('mi.mititle', 'mit_title', 'mitypeTitle'),
    'equipment_modification': ('modification', 'mi.modification'),
    'equipment_serial_number': ('manufactureNum', 'mi.number'),
    'importer_inn': ('consignee_inn', 'importer_tax_id', 'ИНН импортера'),
    'importer_name': ('consignee', 'consignee_name', 'buyer', '采购商'),
    'exporter_name': ('shipper', 'shipper_name', 'supplier', '供应商'),
    'declaration_date': ('shipment_date', 'arrival date', 'date of shipment', '日期'),
    'net_weight_kg': ('net weight (kg)', 'netWgt', '净重'),
    'customs_value_usd': ('value_usd', 'total value usd', '金额美元'),
    'hs_code': ('cmdCode', 'HS/HTS', 'ТН ВЭД', '海关编码'),
    'supervisory_authority': ('Контролирующий орган',),
    'raw_quote': ('quote',),
}


def at(row, path):
    # Literal dotted keys in Solr records take priority over nested paths.
    if path in row:
        return row[path]
    value = row
    for part in path.split('.'):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def objects(payload, path=None):
    if path is not None:
        payload = at(payload, path)
    if isinstance(payload, list) and all(isinstance(v, dict) for v in payload):
        return payload
    if isinstance(payload, dict):
        if payload.get('error') or payload.get('errorMessages') or payload.get('validationError'):
            raise CollectionError('source_reported_error')
        for key in ('data', 'items', 'records', 'contracts', 'shipments', 'cases', 'value', 'docs', 'vacancies'):
            if key in payload:
                return objects(payload[key])
        for key in ('response', 'result', 'results', 'Result'):
            if isinstance(payload.get(key), (dict, list)):
                return objects(payload[key])
        return [payload]
    raise CollectionError('unrecognized_record_container')


def mapped(row, block, mapping=None):
    mapping = mapping or {}
    result = {}
    for field in FIELDS[block]:
        if field in mapping:
            result[field] = at(row, mapping[field])
        else:
            result[field] = get(row, field, *ALIASES.get(field, ()))
    # get() treats dict/list as strings for missing detection, but returns original objects.
    if row.get('source_url'):
        result['source_url'] = row['source_url']
    return result


def load(content, fmt, job):
    if fmt == 'json':
        return objects(json.loads(content), job.get('records_path'))
    if fmt in ('jsonl', 'ndjson'):
        return (json.loads(line) for line in content.splitlines() if line.strip())
    if fmt == 'csv':
        try:
            dialect = csv.Sniffer().sniff(content[:8192], delimiters=',;\t')
        except csv.Error:
            dialect = csv.excel
        return csv.DictReader(io.StringIO(content.lstrip('\ufeff')), dialect=dialect)
    raise CollectionError('unsupported_format:' + fmt)


def selected_html(content, job):
    """Operator-supplied selectors bind fields to record-local nodes, never page-wide INNs."""
    soup = soup_html(content)
    selectors = job.get('selectors')
    if not isinstance(selectors, dict) or not selectors:
        return None
    nodes = soup.select(job['record_selector']) if job.get('record_selector') else [soup]
    rows = []
    for node in nodes:
        row = {}
        for field, selector in selectors.items():
            spec = selector if isinstance(selector, dict) else {'css': selector}
            element = node.select_one(spec['css'])
            row[field] = element.get(spec['attribute']) if element is not None and spec.get('attribute') else element.get_text() if element is not None else None
        rows.append(row)
    return rows


def contract_xml(content):
    root = etree.fromstring(content.encode(), etree.XMLParser(resolve_entities=False, no_network=True))
    def value(node, *names):
        for name in names:
            matches = node.xpath('.//*[local-name()=$n]/text()', n=name)
            if matches:
                return str(matches[0])
        return None
    for node in root.xpath('//*[local-name()="contract"]'):
        customers = node.xpath('.//*[local-name()="customer"]')
        suppliers = node.xpath('.//*[local-name()="supplier" or local-name()="supplierInfo"]')
        customer = customers[0] if len(customers) == 1 else None
        for supplier in suppliers or [None]:
            yield dict(contract_id=value(node, 'regNum', 'regNumber', 'reestrNumber'),
                       contract_date=value(node, 'signDate'), price_rub=value(node, 'price'),
                       subject_text=value(node, 'subject'),
                       customer_inn=value(customer, 'INN', 'inn') if customer is not None else None,
                       customer_name=value(customer, 'fullName', 'name') if customer is not None else None,
                       supplier_inn=value(supplier, 'INN', 'inn') if supplier is not None else None,
                       supplier_name=value(supplier, 'fullName', 'name') if supplier is not None else None,
                       record_kind='contract')


def contracts(rows, job):
    court = job['source'] == 'kad'
    for row in rows:
        base = mapped(row, 1, job.get('column_map'))
        base['record_kind'] = 'court' if court else 'contract'
        if court:
            # Preserve opposing roles; strings are names, never identifiers inferred from a query.
            def parties(value):
                if isinstance(value, list):
                    return value or [None]
                return [value]
            plaintiffs = parties(row.get('Plaintiffs', row.get('plaintiffs')))
            respondents = parties(row.get('Respondents', row.get('respondents')))
            for plaintiff in plaintiffs:
                for respondent in respondents:
                    result = dict(base, party_roles={'customer': 'plaintiff', 'supplier': 'respondent'})
                    for role, party in (('customer', plaintiff), ('supplier', respondent)):
                        if isinstance(party, dict):
                            result[role + '_inn'] = get(party, 'inn', 'Inn', 'ИНН')
                            result[role + '_name'] = get(party, 'name', 'Name', 'fullName')
                        elif isinstance(party, str):
                            result[role + '_name'] = party
                    if result.get('contract_id'):
                        yield result
            continue
        customer = row.get('customer')
        if isinstance(customer, dict):
            base['customer_inn'] = base['customer_inn'] or get(customer, 'inn', 'INN')
            base['customer_name'] = base['customer_name'] or get(customer, 'fullName', 'name')
        currency = row.get('currency')
        if isinstance(currency, dict):
            currency = get(currency, 'code', 'name')
        if base['price_rub'] is None and str(currency).upper() in ('RUB', 'RUR', '643'):
            base['price_rub'] = get(row, 'price', 'amount')
        suppliers = row.get('suppliers')
        if suppliers is None and row.get('supplier_inns'):
            # Separate arrays cannot establish name/identifier pairing.
            suppliers = [{'inn': value} for value in row['supplier_inns']]
        for supplier in suppliers or [None]:
            result = dict(base)
            if isinstance(supplier, dict):
                result['supplier_inn'] = get(supplier, 'inn', 'INN')
                result['supplier_name'] = get(supplier, 'name', 'fullName')
            if result.get('contract_id'):
                yield result


def sentences(body):
    return (part.strip() for part in re.split(r'(?<=[.!?])\s+|\n+', body) if part.strip())


def notices(content, fmt, job, url):
    if fmt == 'html':
        rows = notice_html(content, job['source'], url or '')
    elif fmt in ('json', 'jsonl', 'ndjson') and not job.get('column_map'):
        payload = json.loads(content) if fmt == 'json' else [json.loads(line) for line in content.splitlines() if line.strip()]
        if isinstance(payload, dict) and str(payload.get('status', '200')) != '200':
            raise CollectionError('vacancy_api_reported_error')
        rows = []
        for original in objects(payload, job.get('records_path')):
            original = original.get('vacancy', original)
            converted = notice_json(original, job['source'], url)
            if original.get('id') is not None and len(converted) == 1:
                converted[0]['id'] = original['id']
            rows.extend(converted)
    else:
        rows = load(content, fmt, job)
    for row in rows:
        base = mapped(row, 2, job.get('column_map'))
        base['company_inn'] = base['company_inn'] or row.get('company_tax_id')
        base['record_date'] = base['record_date'] or row.get('posting_date')
        if base['record_id'] is None:
            base['record_id'] = row.get('id')
        if base['record_id'] is None:
            # Source URL carries a published ID on individual notice pages.
            match = re.search(r'/(?:vacancy|vacancies)/(\d+)|/(\d{7,})-', row.get('source_url') or url or '')
            base['record_id'] = next((v for v in match.groups() if v), None) if match else None
        source_url = row.get('source_url') or url
        base['source_site'] = urlsplit(source_url).hostname if source_url else None
        if base.get('text_fragment'):
            body = base['text_fragment']
        else:
            description_soup = BeautifulSoup(str(row.get('description') or ''), 'html.parser')
            for node in description_soup.select('p, div, li, br'):
                node.insert_after('\n')
            description = description_soup.get_text()
            body = '\n'.join(str(v) for v in (row.get('title'), description) if v)
        if job['source'] == 'rostender' and (not MAINTENANCE.search(body) or re.match(r'\s*(поставка|закупка|приобретение)\b', str(row.get('title') or ''), re.I)):
            continue
        for sentence in sentences(body):
            for mention in equipment_mentions(sentence):
                yield dict(base, cnc_brand=mention['brand'], cnc_model=mention['model'],
                           text_fragment=sentence, record_kind='service_tender' if job['source'] == 'rostender' else 'vacancy',
                           source_url=source_url, equipment_type=mention['equipment_type'])


def metrology(rows, job):
    for row in rows:
        result = mapped(row, 3, job.get('column_map'))
        if isinstance(row.get('result'), dict):
            row = row['result']
        info = row.get('vriInfo') or {}
        mi = (row.get('miInfo') or {}).get('singleMI') or {}
        owner = info.get('miOwner')
        if isinstance(owner, dict):
            result.update(facility_name=get(owner, 'name', 'title'), facility_inn=owner.get('inn'))
        elif isinstance(owner, str):
            result['facility_name'] = owner
        if info.get('miOwnerInn') is not None:
            result['facility_inn'] = info['miOwnerInn']
        result['verification_date'] = info.get('vrfDate', result['verification_date'])
        for field, key in [('equipment_type', 'mitypeTitle'), ('equipment_modification', 'modification'), ('equipment_serial_number', 'manufactureNum')]:
            result[field] = mi.get(key, result[field])
        if info.get('inapplicable') is not None:
            result['verification_result'] = 'непридатний'
        elif isinstance(info.get('applicable'), dict):
            result['verification_result'] = 'придатний'
        elif type(row.get('applicability')) is bool:
            result['verification_result'] = 'придатний' if row['applicability'] else 'непридатний'
        if result.get('vri_id') is not None or result.get('equipment_type'):
            yield result


def trade(rows, job):
    for row in rows:
        result = mapped(row, 4, job.get('column_map'))
        if job['source'] == 'comtrade':
            result.update(record_kind='trade_aggregate', period=row.get('period'),
                          reporter_code=row.get('reporterCode'), partner_code=row.get('partnerCode'),
                          flow_code=row.get('flowCode'), aggregate_value_usd=row.get('primaryValue'),
                          aggregate_net_weight_kg=row.get('netWgt'), aggregate_quantity=row.get('qty'),
                          aggregate_quantity_unit_code=row.get('qtyUnitCode'),
                          is_net_weight_estimated=row.get('isNetWgtEstimated'), is_quantity_estimated=row.get('isQtyEstimated'),
                          is_aggregate=row.get('isAggregate'))
            # Comtrade contains country/period totals, not declarations or firm shipments.
            for field in FIELDS[4]:
                if field != 'hs_code':
                    result[field] = None
            yield result
            continue
        result['record_kind'] = 'reported_shipment' if job['source'] == 'c4ads' else 'shipment'
        if job['source'] == 'c4ads':
            result['warnings'] = ['secondary_report_not_primary_customs_declaration']
        quantity = get(row, 'quantity', 'shipment quantity', '数量')
        unit = text(get(row, 'quantity_unit', 'shipment unit', 'unit', '单位'))
        if result.get('units_qty') is None and unit and unit.casefold() in ('pcs', 'pieces', 'units', 'шт', 'шт.', 'штук', '个'):
            result['units_qty'] = quantity
        result['quantity_unit_raw'] = unit
        result['shipment_id'] = get(row, 'shipment_id', 'bill_of_lading', 'bill of lading number')
        if result.get('declaration_date') or result.get('shipment_id'):
            yield result


def patents(content, fmt, job, url):
    if fmt == 'html':
        patent = patent_html(content, url or '')
        # Keep exact visible paragraph text rather than patent parser's whitespace cleanup.
        soup = soup_html(content)
        sections = {}
        for section, selector in [('claims', 'section[itemprop="claims"] .claim-text'), ('description', 'section[itemprop="description"] .description-paragraph')]:
            nodes = soup.select(selector)
            if section == 'claims':
                nodes = [node for node in nodes if not node.find_parent(class_='claim-text')]
            if not nodes:
                nodes = soup.select('section[itemprop="' + section + '"] [itemprop="content"]')
            if nodes:
                sections[section] = [node.get_text() for node in nodes]
        if sections:
            patent.sections = sections
        elif job['source'] == 'fips':
            visible = soup.get_text('\n', strip=False)
            headings = list(re.finditer(r'^\s*(Формула\s+(?:изобретения|полезной модели)|Описание\s+(?:изобретения|полезной модели))\s*[:.]?\s*$', visible, re.I | re.M))
            patent.sections = {}
            for index, heading in enumerate(headings):
                section = 'claims' if heading[1].casefold().startswith('формула') else 'description'
                snippet = visible[heading.end():headings[index + 1].start() if index + 1 < len(headings) else len(visible)]
                if snippet.strip():
                    patent.sections.setdefault(section, []).append(snippet)
        patents_list = [patent]
    elif fmt == 'xml':
        patent = patent_xml(content, url or '')
        root = etree.fromstring(content.encode(), etree.XMLParser(resolve_entities=False, no_network=True))
        patent.sections = {}
        for section, expression in [('claims', '//*[local-name()="claims"]/*[local-name()="claim"]'), ('description', '//*[local-name()="description"]/*[local-name()="p"]')]:
            patent.sections[section] = [''.join(node.itertext()) for node in root.xpath(expression)]
        patents_list = [patent]
    else:
        patents_list = []
        for row in load(content, fmt, job):
            # Flat verified excerpts are accepted without inventing full claims.
            if row.get('raw_snippet_quote'):
                yield mapped(row, 5, job.get('column_map'))
                continue
            patent = patent_json(row)
            for section in ('claims', 'description'):
                value = row.get(section, row.get('sections', {}).get(section, []))
                patent.sections[section] = value if isinstance(value, list) else value.splitlines()
            patents_list.append(patent)
    for patent in patents_list:
        if not re.fullmatch(r'[A-Z]{2}\d+[A-Z]\d?', patent.patent_number):
            raise CollectionError('patent_identifier_missing_kind_code')
        emitted = False
        for section, paragraphs in patent.sections.items():
            for snippet in paragraphs:
                channels, multipliers, reasons = analyze(snippet)
                # Existing analyzer accepts a singular component as one. This pipeline
                # requires an explicit numeral/quantity word for a numeric multiplier.
                if multipliers:
                    explicit = {}
                    for component, multiplier in multipliers.items():
                        match = COMPONENTS[component].search(snippet)
                        prefix = snippet[max(0, match.start() - 40):match.start()] if match else ''
                        quantity = re.search(r'(\d+|' + '|'.join(WORDS) + r'|один|одна|одно)\s*$', prefix, re.I)
                        if quantity:
                            token = quantity[1].casefold()
                            amount = int(token) if token.isdigit() else WORDS.get(token, 1)
                            explicit[component] = dict(multiplier, per_channel=amount)
                    multipliers = explicit or None
                    if not multipliers:
                        reasons.append('component_quantity_not_explicit')
                channel_count = channels['values'][0] if len(channels['values']) == 1 and not any(r in reasons for r in ('bounded_or_range_count', 'prior_art_context', 'negated_context')) else None
                for assignee in patent.assignees or [None]:
                    # One record per explicit component relation; never assume one chip per channel.
                    components = multipliers or {None: None}
                    for component, multiplier in components.items():
                        yield dict(patent_number=patent.patent_number, assignee_name=assignee,
                                   publication_date=patent.publication_date, device_title=patent.device_title,
                                   detected_architecture_channels=channel_count,
                                   calculated_component_multiplier=multiplier['per_channel'] if multiplier else None,
                                   component_name=component, multiplier_basis='per_explicit_channel' if multiplier else None,
                                   raw_snippet_quote=snippet, section=section, review_reasons=reasons,
                                   interpretation='functional_architecture_not_verified_chip_bom')
                        emitted = True
        if not emitted:
            yield dict(patent_number=patent.patent_number, assignee_name=patent.assignees[0] if len(patent.assignees) == 1 else None,
                       publication_date=patent.publication_date, device_title=patent.device_title)


def spatial(rows, job):
    for row in rows:
        result = mapped(row, 6, job.get('column_map'))
        if job['source'] == 'firms':
            try:
                result['facility_coords'] = [float(row['latitude']), float(row['longitude'])]
            except (KeyError, TypeError, ValueError):
                pass
            day, raw_clock = row.get('acq_date'), text(row.get('acq_time'))
            clock = raw_clock.zfill(4) if raw_clock and re.fullmatch(r'\d{1,4}', raw_clock) else None
            if day and clock:
                result['t_event'] = f'{day}T{clock[:2]}:{clock[2:]}:00Z'
            result.update(t_recovery=None, damage_type=None, scene_id=None,
                          confidence_flags={k: row.get(k) for k in ('confidence', 'instrument', 'satellite', 'scan', 'track')},
                          observation_kind='thermal_detection', coordinate_role='detection_pixel_not_identified_facility')
        elif job['source'] == 'copernicus':
            attrs = {v.get('Name'): v.get('Value') for v in row.get('Attributes', []) if isinstance(v, dict)}
            result.update(scene_id=row.get('Id'), scene_name=row.get('Name'),
                          observation_time=(row.get('ContentDate') or {}).get('Start'),
                          footprint=row.get('GeoFootprint'),
                          confidence_flags={'cloud_cover': attrs.get('cloudCover'), 'resolution': attrs.get('resolution')},
                          observation_kind='scene_catalog_metadata', t_event=None, t_recovery=None, damage_type=None)
        elif result.get('damage_type') or result.get('t_recovery'):
            result['warnings'] = ['reported_incident_or_recovery_requires_independent_image_review']
        if result.get('scene_id') or result.get('t_event') or result.get('facility_coords'):
            yield result


def parse(content, fmt, job, url):
    block, source = job['block'], job['source']
    if source not in SOURCES.get(block, ()):
        raise CollectionError('source_not_supported_for_block')
    if fmt == 'html':
        selected = selected_html(content, job)
        if selected is not None:
            rows = selected
        elif block == 1:
            if source == 'kad':
                from parsers.registry_sources import KadParser
                parsed = KadParser().parse(content, 'html', url)
                rows = [record.data for record in parsed.records]
            else:
                from contract_links_async import parse_zakupki_card, parse_contract_html
                soup_html(content)
                models = parse_zakupki_card(content, url or '') if source == 'zakupki' else [parse_contract_html(content)]
                rows = [model.model_dump(mode='json') for model in models]
        elif block == 2:
            yield from notices(content, fmt, job, url)
            return
        elif block == 5:
            yield from patents(content, fmt, job, url)
            return
        elif block in (4, 7):
            rows = table_rows(soup_html(content))
            if not rows:
                raise CollectionError('html_requires_record_selector_and_field_selectors_or_structured_export')
        else:
            raise CollectionError('html_requires_record_selector_and_field_selectors_or_structured_export')
    elif fmt == 'xml' and block == 1:
        yield from contract_xml(content)
        return
    elif block == 2:
        yield from notices(content, fmt, job, url)
        return
    elif block == 5 and not job.get('column_map'):
        yield from patents(content, fmt, job, url)
        return
    else:
        rows = load(content, fmt, job)
    if block == 1:
        yield from contracts(rows, job)
    elif block == 3:
        yield from metrology(rows, job)
    elif block == 4:
        yield from trade(rows, job)
    elif block == 6:
        yield from spatial(rows, job)
    else:
        for row in rows:
            result = mapped(row, block, job.get('column_map'))
            if any(value is not None for value in result.values()):
                yield result
