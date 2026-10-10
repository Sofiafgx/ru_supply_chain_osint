"""Link local contract/vacancy equipment mentions without fabricating verifications."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import sqlite3

from bs4 import BeautifulSoup
from arshin_parser import normalized_name
from parsers.industrial_labor import html_rows, json_rows, notice_date

BRANDS = {
    'Zeiss': r'Zeiss|Цейсс', 'Renishaw': r'Renishaw|Ренишоу',
    'Mitutoyo': r'Mitutoyo|Митутойо', 'Heidenhain': r'Heidenhain|Хайденхайн',
    'Marposs': r'Marposs', 'DMG Mori': r'DMG[\s-]*Mori', 'Haas': r'Haas|Хаас',
    'Fanuc': r'Fanuc|Фанук', 'Siemens': r'Siemens|Сименс',
}
MODELS = {
    'Haas': r'(?:VF|VM|DT|DM|ST|SL|UMC|EC|DS|TL|TM|GR|VR)[\s-]*\d+[A-Za-z0-9/-]*(?:\s+(?:SSY|SS))?',
    'DMG Mori': r'(?:DMC|DMU|DMF|CTX|NLX|NTX|CMX|CLX|NHX|NZX)[\s-]*\d+[A-Za-z0-9/-]*',
    'Heidenhain': r'(?:ND|LS|LC|TNC|iTNC|ERN|ROD|LIDA)[\s-]*\d+[A-Za-z0-9/-]*',
    'Renishaw': r'(?:XL|XM|XR|QC)[\s-]*\d+[A-Za-z0-9/-]*|Equator(?:\s+\d+)?',
    'Zeiss': r'(?:PRISMO|CONTURA|ACCURA|MICURA|DuraMax|T[\s-]*SCAN)(?:\s+\d+)?',
    'Mitutoyo': r'(?:CRYSTA[\s-]*APEX|STRATO[\s-]*APEX|LEGEX)(?:\s+[A-Za-z0-9-]+)?',
    'Fanuc': r'(?:\d{1,3}|0)i[A-Za-z0-9-]*',
    'Siemens': r'(?:SINUMERIK(?:\s+8[024]\d[A-Za-z]*)?|8[024]\d[A-Za-z]*)',
}
CATEGORIES = {
    'coordinate_measuring_machine': r'координатно[\s-]*измеритель\w*|\bКИМ\b',
    'interferometer': r'интерферометр\w*',
    'balancing_stand': r'балансировоч\w*\s+(?:стенд|станок)\w*',
    'cnc_control': r'(?:систем\w*|стойк\w*)\s+(?:с\s+)?ЧПУ',
    'machine_tool': r'\bЧПУ\b|металлообрабатывающ\w*\s+стан\w*|'
                    r'(?:токарн|фрезерн|шлифовальн)\w*\s+стан\w*|обрабатывающ\w*\s+центр',
}
HINTS = ('zeiss', 'цейсс', 'renishaw', 'ренишоу', 'mitutoyo', 'митутойо',
         'heidenhain', 'хайденхайн', 'marposs', 'dmg', 'haas', 'хаас', 'fanuc',
         'фанук', 'siemens', 'сименс', 'координатно', 'ким', 'интерферометр',
         'балансировоч', 'чпу', 'металлообрабатыва', 'токарн', 'фрезерн',
         'шлифовальн', 'обрабатывающ')


def mentions(text):
    """Models come from literal equipment text, never a catalogue Id.Nr. or a date."""
    lowered = text.casefold()
    if not any(hint in lowered for hint in HINTS):
        return []
    found = []
    for brand, pattern in BRANDS.items():
        matches = list(re.finditer(r'(?<!\w)(?:' + pattern + r')(?!\w)', text, re.I))
        if not matches:
            continue
        model_pattern = MODELS.get(brand)
        models = list(re.finditer(r'(?<!\w)(?:' + model_pattern + r')(?!\w)', text, re.I)) if model_pattern else []
        # Product lines may put the model before the brand (e.g. LS 487C ... HEIDENHAIN).
        if not models:
            found.append((brand, None, 'brand_mention', matches[0].start()))
        else:
            for model in models:
                found.append((brand, model[0], 'model_mention', model.start()))
    if not found:
        for category, pattern in CATEGORIES.items():
            match = re.search(pattern, text, re.I)
            if match:
                explicit = re.search(r'\bмодел[ьи]\s+([A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9/-]*\d[A-Za-zА-Яа-яЁё0-9/-]*)',text,re.I)
                found.append((None, explicit[1] if explicit else None, category, match.start()))
                break
    return found


def exact_contract(batch, ref):
    rows = [row for row in batch.get('data', []) if str(row.get('regnum')) == str(ref)]
    if len(rows) != 1:
        raise ValueError(f'Expected one source card for contract {ref}; found {len(rows)}')
    return rows[0]


def relation(text):
    if re.search(r'поверк|калибров|метролог', text, re.I):
        return 'metrology_service_contract'
    if re.search(r'ремонт|диагност|обслужив', text, re.I):
        return 'maintenance_contract'
    return 'procurement_contract'


def enrich_links(links):
    """Keep catalogue/inventory numbers separate from serial numbers and retain variants."""
    cache = {}
    for row in links:
        text = row['evidence_excerpt']
        if row['equipment_model'] is None and row['equipment_brand'] is None:
            found = mentions(text)
            if found and found[0][1]:
                row['equipment_model'] = found[0][1]
        inventory = re.search(r'\bинв\.?\s*(?:№\s*)?([0-9][A-Za-zА-Яа-я0-9/-]*)',text,re.I)
        catalogue = re.search(r'Id\.?\s*Nr\.?\s*[:№]?\s*(\d[\d ]*-\d+)',text,re.I)
        modification = re.search(r'\bисп\.?\s*(\d+[A-Za-zА-Яа-я0-9/-]*)',text,re.I)
        row['equipment_inventory_number'] = inventory[1] if inventory else None
        row['equipment_catalogue_number'] = catalogue[1].strip() if catalogue else None
        row['equipment_modification'] = 'исп. '+modification[1] if modification else None
        row['supporting_equipment_descriptions'] = [text]
        if row['is_target_company'] and row['source_kind']=='contract' and row['equipment_brand']:
            path=row['input_file']
            if path not in cache:
                cache[path]=json.loads(Path(path).read_text())
            card=exact_contract(cache[path],row['source_record_id'])
            matching=[]
            for product in card.get('product_names',[]):
                if any(brand==row['equipment_brand'] and normalized_name(model or '')==normalized_name(row['equipment_model'] or '')
                       for brand,model,_,_ in mentions(product)):
                    matching.append(product)
            if matching:
                row['supporting_equipment_descriptions']=list(dict.fromkeys(matching))


def build(root, destination):
    destination.mkdir(parents=True, exist_ok=True)
    aliases = json.loads((root / 'config/facility_names.json').read_text())
    requested_inns = set(aliases)
    reference = json.loads((root / 'config/russian_defence_entities.json').read_text())['entities']
    verified = json.loads((root / 'data/compliance_offline_20261009/verified_tax_ids.json').read_text())
    target_inns = requested_inns | {e['inn'] for e in reference} | {e['inn'] for e in verified}
    target_names = {normalized_name(a) for aa in aliases.values() for a in aa}
    target_names |= {normalized_name(e['name']) for e in reference}
    target_names |= {normalized_name(e['company']) for e in verified}
    links, seen = [], set()
    coverage = dict(started_at=datetime.now(timezone.utc).isoformat(), contract_cards=0,
                    raw_batch_files=0, notice_files=0, notice_cards=0, errors=[])

    def add(name, inn, text, proof, relation_type):
        for brand, model, category, position in mentions(text):
            key = (proof['source_record_id'], proof['source_kind'], name, inn, brand,
                   normalized_name(model) if model else None, category)
            if key in seen:
                continue
            seen.add(key)
            links.append(dict(company_name=html.unescape(name) if name else None,
                              company_inn=inn or None,
                              is_target_company=inn in target_inns or (bool(name) and normalized_name(html.unescape(name)) in target_names),
                              identity_basis='source_inn' if inn else 'source_name_only' if name else 'unknown',
                              equipment_brand=brand, equipment_model=model,
                              equipment_type=category, equipment_serial_number=None,
                              relation_type=relation_type,
                              ownership_confirmed=False, verification_confirmed=False,
                              equipment_mention_confirmed=True,
                              confirmation_basis='explicit_mention_in_local_source_card',
                              verification_date=None, verification_result=None, vri_id=None,
                              evidence_excerpt=text[max(0, position-100):position+350], **proof))

    db = root / 'data/compliance_offline_20261009/processed/offline_contracts.sqlite'
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    grouped = defaultdict(list)
    for row in con.execute('SELECT * FROM contracts'):
        grouped[row['input_file']].append(dict(row))
    con.close()
    indexed_total = sum(len(rows) for rows in grouped.values())
    for path in (root/'data/collection_20261008/raw/clearspending').glob('*.json'):
        grouped.setdefault(str(path), [])
    contracts_seen = set()
    for index, (filename, rows) in enumerate(grouped.items(), 1):
        path = Path(filename)
        try:
            raw = path.read_bytes()
            batch = json.loads(raw)
            digest = hashlib.sha256(raw).hexdigest()
            indexed = defaultdict(list)
            for card in batch.get('data', []):
                indexed[str(card.get('regnum'))].append(card)
            coverage['raw_batch_files'] += 1
            for row in rows:
                matches = indexed[row['contract_ref_id']]
                if len(matches) != 1:
                    raise ValueError('Source card missing or duplicated: '+row['contract_ref_id'])
                card = matches[0]
                if str(card.get('customer_inn') or '') != str(row.get('customer_tax_id') or ''):
                    raise ValueError('Customer INN differs from index: '+row['contract_ref_id'])
            index_rows = {row['contract_ref_id']:row for row in rows}
            for card in batch.get('data', []):
                ref = str(card.get('regnum') or '')
                if not ref:
                    raise ValueError('Raw card without regnum')
                if ref in contracts_seen:
                    continue
                contracts_seen.add(ref)
                coverage['contract_cards'] += 1
                source_url = index_rows.get(ref,{}).get('source_url') or f'https://clearspending.ru/contracts/{card.get("fz")}/{ref}'
                proof = dict(source_kind='contract', source_record_id=ref,
                             source_date=card.get('sign_date'), source_url=source_url,
                             input_file=str(path), input_sha256=digest)
                subject = card.get('subject') or ''
                # Scope equipment to this card's subject/products, never the whole batch.
                for text in dict.fromkeys([subject] + (card.get('product_names') or [])):
                    add(card.get('customer_name'), card.get('customer_inn'), str(text), proof, relation(subject+'\n'+str(text)))
        except (ValueError, OSError, TypeError) as exc:
            coverage['errors'].append(dict(file=str(path), error=str(exc)))
        if index % 200 == 0:
            print(f'Batch files {index}/{len(grouped)}; cards {coverage["contract_cards"]}; links {len(links)}', flush=True)

    for folder in ('data/industrial_20261009/raw', 'data/industrial_20261009_followup/raw'):
        for path in sorted((root/folder).iterdir()):
            if path.suffix not in ('.json', '.html'):
                continue
            coverage['notice_files'] += 1
            source = next((s for s in ('fkc','hh','joblab','rostender','cz') if path.name.startswith(s)), None)
            source = 'cznelb' if source == 'cz' else source
            if not source:
                continue
            try:
                raw = path.read_bytes()
                if path.suffix == '.json':
                    payload = json.loads(raw)
                    if source == 'fkc' and 'VacancySend' not in raw.decode('utf-8'):
                        continue
                    rows = json_rows(payload, source)
                else:
                    match = re.search(r'(\d{7,})',path.stem)
                    url = f'https://hh.ru/vacancy/{match[1]}' if source=='hh' and match else ''
                    rows = html_rows(raw.decode('utf-8'), source, url)
                for row in rows:
                    if not row.get('title') or not row.get('source_url'):
                        continue
                    coverage['notice_cards'] += 1
                    text = row['title']+'\n'+BeautifulSoup(row.get('description') or '', 'html.parser').get_text(' ',strip=True)
                    proof = dict(source_kind='vacancy' if source!='rostender' else 'tender',
                                 source_record_id=row['source_url'], source_date=notice_date(row.get('posting_date')),
                                 source_url=row['source_url'], input_file=str(path), input_sha256=hashlib.sha256(raw).hexdigest())
                    add(row.get('company_name'),row.get('company_tax_id'),text,proof,
                        'vacancy_mention' if source!='rostender' else 'tender_mention')
            except (ValueError, OSError, TypeError, RuntimeError) as exc:
                coverage['errors'].append(dict(file=str(path),error=str(exc)))

    # Include previously parsed individual/listing evidence (Joblab/Rostender URL maps).
    for folder in ('data/industrial_20261009','data/industrial_20261009_followup'):
        recordpath, evidencepath = root/folder/'records.json', root/folder/'records.evidence.json'
        records = json.loads(recordpath.read_text()); proofs = json.loads(evidencepath.read_text())
        for row, evidence in zip(records,proofs):
            for mention in evidence.get('equipment_mentions',[]):
                proof = dict(source_kind='tender' if evidence['source']=='rostender' else 'vacancy',
                             source_record_id=row['source_url'],source_date=row.get('posting_date'),
                             source_url=row['source_url'],input_file=evidence.get('input_file'),input_sha256=evidence.get('input_sha256'))
                add(evidence.get('company_name'),row.get('company_tax_id'),mention.get('context') or mention['raw_mention'],
                    proof,'tender_mention' if evidence['source']=='rostender' else 'vacancy_mention')

    enrich_links(links)
    equipment_path = destination/'enterprise_equipment_links.jsonl'
    equipment_path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in links),encoding='utf-8')
    targetlinks=[r for r in links if r['is_target_company']]
    (destination/'target_enterprise_equipment.json').write_text(json.dumps(targetlinks,ensure_ascii=False,indent=2)+'\n')
    bycompany=defaultdict(list)
    for row in links:
        key=row['company_inn'] or ('name:'+normalized_name(row['company_name'])) if row['company_name'] or row['company_inn'] else 'unknown'
        bycompany[key].append(row)
    companies=[]
    for key,rows in bycompany.items():
        companies.append(dict(company_key=key,company_name=rows[0]['company_name'],company_inn=rows[0]['company_inn'],
                              is_target_company=any(r['is_target_company'] for r in rows),equipment_mentions=len(rows),
                              source_cards=len({r['source_record_id'] for r in rows}),
                              equipment=[dict(brand=r['equipment_brand'],model=r['equipment_model'],type=r['equipment_type'],
                                              relation=r['relation_type'],source_url=r['source_url'],
                                              descriptions=r['supporting_equipment_descriptions'],
                                              inventory_number=r['equipment_inventory_number'],
                                              catalogue_number=r['equipment_catalogue_number']) for r in rows]))
    (destination/'enterprise_equipment_map.json').write_text(json.dumps(companies,ensure_ascii=False,indent=2)+'\n')
    coverage.update(finished_at=datetime.now(timezone.utc).isoformat(),links=len(links),companies=len(companies),
                    target_links=len(targetlinks),target_companies=len({r['company_inn'] or r['company_name'] for r in targetlinks}),
                    verified_verifications=0,relation_counts=dict(Counter(r['relation_type'] for r in links)),
                    contracts_indexed=indexed_total,
                    scope='All local ClearSpending raw batch files plus indexed contracts, both industrial raw folders and their parsed evidence. No online requests; no inference from court participation or social mentions.',
                    classification='Contracts/vacancies are evidence of procurement/service requests/mentions, not completed verification or confirmed current ownership.')
    (destination/'coverage.json').write_text(json.dumps(coverage,ensure_ascii=False,indent=2)+'\n')
    lines=['# Підприємства та обладнання: локальні картки', '',
           f'Опрацьовано {coverage["contract_cards"]} контрактних карток, {coverage["notice_cards"]} спостережень карток вакансій/тендерів.',
           f'Знайдено {len(links)} зв’язків-згадок у {len(companies)} групах підприємств (включно з невстановленим замовником).',
           'Це згадки закупівлі, ремонтних/метрологічних замовлень і вакансій. Завершення послуг та поточна власність не підтверджені.',
           'Дата контракту/публікації не є датою повірки; каталожний Id.Nr. не є серійним номером.', '',
           '## Цільові підприємства', '', '| Підприємство | ІПН із джерела | Обладнання | Зв’язок | Джерело |', '|---|---|---|---|---|']
    for row in targetlinks:
        eq=' '.join(filter(None,[row['equipment_brand'],row['equipment_model']])) or row['equipment_type']
        values=[row['company_name'] or 'не встановлено',row['company_inn'] or 'не встановлено',eq,row['relation_type'],f'[картка]({row["source_url"]})']
        lines.append('| '+' | '.join(str(v).replace('|','/').replace('\n',' ') for v in values)+' |')
    if not targetlinks:lines.append('| Підтверджених зв’язків за назвою/ІПН не знайдено | | | | |')
    lines.extend(['','Список охоплює лише локальний архів. Відсутність згадки не доводить відсутності обладнання.',
                  'ІПН береться з картки. Розбіжності з користувацьким довідником не виправляються шляхом підміни номера.',
                  f'Помилок читання/зіставлення: {len(coverage["errors"])}. Деталі — у coverage.json.'])
    (destination/'REPORT_UK.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps(coverage,ensure_ascii=False))


if __name__=='__main__':
    build(Path.cwd(),Path.cwd()/'outputs/enterprise_equipment_20261010')
