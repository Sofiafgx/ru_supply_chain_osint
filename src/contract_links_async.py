"""Collect evidenced, directed contracts between target INNs. Python 3.9+."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import itertools
import json
import logging
import os
import random
import ssl
import re
import tempfile
import time
from contextlib import AsyncExitStack
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Literal, Optional
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, StrictStr

TARGETS = ['1646019864', '2128700223', '1804002498', '6612001460', '7719830028', '6444009038']
INN = Annotated[StrictStr, Field(pattern=r'^(?:[0-9]{10}|[0-9]{12})$')]
Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
LOG = logging.getLogger('contract_links')


class AuditFields(BaseModel):
    input_sha256: Optional[StrictStr] = None
    collected_at: Optional[StrictStr] = None
    source_url: Optional[StrictStr] = None
    verified: Optional[bool] = None


class Contract(AuditFields):
    model_config = ConfigDict(extra='forbid')
    # All fields are required, but nullable: no replacement values.
    customer_inn: Optional[INN]
    customer_name: Optional[StrictStr]
    supplier_inn: Optional[INN]
    supplier_name: Optional[StrictStr]
    contract_id: Optional[StrictStr]
    contract_date: Optional[date]
    price_rub: Optional[Money]
    subject_text: Optional[StrictStr]


class ExecutionEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    customer_inn: Optional[INN]
    supplier_inn: Optional[INN]
    contract_id: Optional[StrictStr]
    stage_text: Optional[StrictStr]
    acceptance_text: Optional[StrictStr]
    payment_text: Optional[StrictStr]
    source_url: StrictStr


class CourtCase(AuditFields):
    model_config = ConfigDict(extra='forbid')
    party_a_inn: INN
    party_b_inn: INN
    plaintiff_inn: INN
    respondent_inn: INN
    case_number: Optional[StrictStr]
    registration_date: Optional[date]
    claim_amount_rub: Optional[Money]
    dispute_text: Optional[StrictStr]
    source_url: StrictStr
    evidence_urls: list[StrictStr]
    # Keyword evidence is not a finding of liability or a contract match.
    dispute_evidence: Literal['keyword_candidate', 'not_available']


def clean(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('Expected source string')
    return value.strip() or None


def tax_id(value):
    value = clean(value)
    if value is not None and not re.fullmatch(r'(?:[0-9]{10}|[0-9]{12})', value):
        raise ValueError('Invalid source INN')
    return value


def parse_date(value):
    value = clean(value)
    if value is None:
        return None
    for fmt in ('%Y-%m-%d', '%d.%m.%Y'):
        try:
            return datetime.strptime(value[:10], fmt).date()
        except ValueError:
            pass
    raise ValueError('Invalid source date')


def money(value):
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise ValueError('Boolean is not money')
    if isinstance(value, str):
        value = re.sub(r'\s|\u00a0|₽|руб(?:лей|ля|ль|\.)?', '', value, flags=re.I).replace(',', '.')
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('Invalid source amount') from None
    if not amount.is_finite() or amount < 0:
        raise ValueError('Invalid source amount')
    return amount


def first(row, *keys):
    return next((row[k] for k in keys if row.get(k) is not None), None)


def json_line(model: BaseModel) -> str:
    """Encode Decimal as an exact JSON number, dates as ISO strings, nulls included."""
    def encode(value):
        if value is None:
            return 'null'
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, date):
            return json.dumps(value.isoformat())
        if isinstance(value, dict):
            return '{' + ','.join(json.dumps(k) + ':' + encode(v) for k, v in value.items()) + '}'
        if isinstance(value, list):
            return '[' + ','.join(encode(v) for v in value) + ']'
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    data = model.model_dump(mode='python')
    for key in ('input_sha256', 'collected_at', 'source_url', 'verified'):
        if key in data and data[key] is None and isinstance(model, AuditFields):
            del data[key]
    return encode(data) + '\n'


class AccessBlocked(RuntimeError):
    pass


class SourceChanged(RuntimeError):
    pass


class ProfileNotFound(RuntimeError):
    pass


class Transport:
    """Fail closed. Every request and redirect uses an explicit proxy; no direct client."""
    HOSTS = {'checko.ru', 'www.checko.ru', 'clearspending.ru', 'www.clearspending.ru',
             'kad.arbitr.ru', 'ras.arbitr.ru', 'zakupki.gov.ru', 'www.zakupki.gov.ru'}

    def __init__(self, proxies: list[str], *, concurrency=4, interval=2.0, attempts=4,
                 raw_dir: Path | None = None, factory=httpx.AsyncClient):
        if not proxies:
            raise ValueError('A nonempty proxy list is required')
        self.proxies = list(dict.fromkeys(proxies))
        for proxy in self.proxies:
            p = urlparse(proxy)
            if p.scheme not in ('http', 'https', 'socks5') or not p.hostname or not p.port:
                raise ValueError('Proxy must be a complete http/https/socks5 URL with port')
        self.cycle = itertools.cycle(range(len(self.proxies)))
        self.sem = asyncio.Semaphore(concurrency)
        self.interval, self.attempts = interval, attempts
        self.lock = asyncio.Lock()
        self.next_time = {}
        self.raw_dir, self.factory = raw_dir, factory
        self.stack = AsyncExitStack()

    async def __aenter__(self):
        self.clients = []
        try:
            for proxy in self.proxies:
                # HTTP CONNECT proxies support TLS to the destination too.
                self.clients.append(await self.stack.enter_async_context(self.factory(
                    proxy=(httpx.Proxy(proxy, ssl_context=ssl._create_unverified_context())
                           if urlparse(proxy).scheme == 'https' else proxy), verify=False, trust_env=False, follow_redirects=False,
                    timeout=httpx.Timeout(40, connect=15),
                    limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                    headers={'User-Agent': 'ContractResearch/1.0', 'Accept': '*/*'})))
        except BaseException:
            await self.stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc):
        await self.stack.aclose()

    @classmethod
    def allowed(cls, url):
        p = urlparse(url)
        if p.scheme != 'https' or p.hostname not in cls.HOSTS or p.username or p.password or p.port not in (None, 443):
            raise ValueError('Unapproved destination')
        return url

    async def pace(self, host):
        async with self.lock:
            now = time.monotonic()
            slot = max(now, self.next_time.get(host, now))
            self.next_time[host] = slot + self.interval
        await asyncio.sleep(max(0, slot - time.monotonic()))

    async def request(self, method, url, **kwargs):
        self.allowed(url)
        for redirect in range(6):
            response = None
            for attempt in range(self.attempts):
                idx = next(self.cycle)
                await self.pace(urlparse(url).hostname)
                try:
                    async with self.sem:
                        response = await self.clients[idx].request(method, url, **kwargs)
                    if response.status_code in (401, 403):
                        raise AccessBlocked(f'HTTP {response.status_code}; access restricted')
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt + 1 == self.attempts:
                            raise RuntimeError(f'HTTP {response.status_code}; retries exhausted')
                        try:
                            retry_after = min(60, max(0, float(response.headers.get('Retry-After', '0'))))
                        except ValueError:
                            retry_after = 0
                        await asyncio.sleep(max(retry_after, 2 ** attempt + random.random()))
                        continue
                    if not response.is_redirect:
                        response.raise_for_status()
                    break
                except httpx.TransportError:
                    if attempt + 1 == self.attempts:
                        # Do not leak proxy credentials from exception messages.
                        raise RuntimeError('Proxy/network retries exhausted') from None
                    await asyncio.sleep(2 ** attempt + random.random())
            assert response is not None
            if response.is_redirect:
                url = self.allowed(urljoin(url, response.headers['location']))
                if response.status_code == 303 or response.status_code in (301, 302) and method == 'POST':
                    method = 'GET'
                    kwargs.pop('json', None)
                    kwargs.pop('data', None)
                kwargs.pop('params', None)
                continue
            if 'html' in response.headers.get('content-type', ''):
                soup = BeautifulSoup(response.text, 'html.parser')
                visible = soup.get_text(' ', strip=True).lower()
                if any(t in visible for t in ('access denied', 'oh noes!', 'подтвердите, что вы не робот',
                                              'доступ заблокирован', 'проверка браузера')) or soup.select_one('form[action*="captcha"], #anubis_challenge'):
                    raise AccessBlocked('Anti-bot challenge; not an empty result')
            if self.raw_dir:
                await asyncio.to_thread(self.archive, response)
            return response
        raise RuntimeError('Redirect limit exceeded')

    def archive(self, response):
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(response.content).hexdigest()
        (self.raw_dir / f'{digest}.bin').write_bytes(response.content)
        from collector_archive import archive_response
        response.extensions['evidence_provenance'] = archive_response(self.raw_dir / 'responses.jsonl', response)


def normalize_contract(row):
    """Clearspending public response. Do not zip parallel supplier names/INNs."""
    suppliers = row.get('suppliers')
    if suppliers is None:
        inns = row.get('supplier_inns')
        if inns is None:
            inns = [row.get('supplier_inn')]
        names = row.get('supplier_names')
        suppliers = [{'inn': v, 'name': (names[0] if len(inns) == 1 and isinstance(names, list)
                      and len(names) == 1 else row.get('supplier_name') if len(inns) == 1 else None)} for v in inns]
    if not isinstance(suppliers, list):
        raise SourceChanged('Unknown supplier structure')
    customer = row.get('customer') or {}
    if not isinstance(customer, dict):
        raise SourceChanged('Unknown customer object')
    currency = row.get('currency')
    if isinstance(currency, dict):
        currency = first(currency, 'code', 'name')
    price = first(row, 'price_rub', 'amount_rur')
    if price is None and currency in ('RUB', 'RUR', '643'):
        price = first(row, 'price', 'amount')
    records = []
    for supplier in suppliers:
        if not isinstance(supplier, dict):
            raise SourceChanged('Unknown supplier object')
        records.append(Contract(
            customer_inn=tax_id(first(row, 'customer_inn') or first(customer, 'inn', 'ИНН')),
            customer_name=clean(first(row, 'customer_name') or first(customer, 'name', 'fullName')),
            supplier_inn=tax_id(first(supplier, 'inn', 'ИНН')),
            supplier_name=clean(first(supplier, 'name', 'fullName')),
            contract_id=clean(first(row, 'regnum', 'regNum', 'contract_id', 'contract_number')),
            contract_date=parse_date(first(row, 'sign_date', 'signDate', 'contract_date')),
            price_rub=money(price), subject_text=clean(first(row, 'subject', 'subject_text'))))
    return records


def labelled(soup, *labels):
    for node in soup.select('tr, dt, .label, .field-label, .blockInfo__title, .section__title'):
        if node.name == 'tr':
            cells = node.find_all(['th', 'td'], recursive=False)
            if len(cells) >= 2 and cells[0].get_text(' ', strip=True).rstrip(':') in labels:
                return cells[1].get_text(' ', strip=True) or None
        elif node.get_text(' ', strip=True).rstrip(':') in labels:
            value = node.find_next_sibling()
            if value:
                return value.get_text(' ', strip=True) or None
    return None


def parse_contract_html(html):
    soup = BeautifulSoup(html, 'html.parser')
    # Only explicit party labels; never take arbitrary INNs from the whole page.
    customer = labelled(soup, 'ИНН заказчика')
    supplier = labelled(soup, 'ИНН поставщика', 'ИНН исполнителя')
    for role, labels in (('customer', ('Заказчик',)), ('supplier', ('Поставщик', 'Исполнитель'))):
        section = soup.select_one(f'[data-role="{role}"], #{role}, .{role}')
        if section is not None:
            value = labelled(section, 'ИНН')
            if role == 'customer':
                customer = customer or value
            else:
                supplier = supplier or value
    return Contract(customer_inn=tax_id(customer), supplier_inn=tax_id(supplier),
        customer_name=clean(labelled(soup, 'Наименование заказчика', 'Заказчик')),
        supplier_name=clean(labelled(soup, 'Наименование поставщика', 'Поставщик', 'Исполнитель')),
        contract_id=clean(labelled(soup, 'Реестровый номер контракта', 'Номер контракта')),
        contract_date=parse_date(labelled(soup, 'Дата заключения контракта', 'Дата заключения')),
        price_rub=money(labelled(soup, 'Цена контракта, руб.', 'Цена контракта (руб.)', 'Цена контракта')),
        subject_text=clean(labelled(soup, 'Предмет контракта', 'Предмет договора', 'Объект закупки')))


def annotate(model, response):
    from eis_ftp_contracts import valid_inn
    audit = response.extensions.get('evidence_provenance') or dict(
        input_sha256=hashlib.sha256(response.content).hexdigest(),
        collected_at=datetime.now(timezone.utc).isoformat(), source_url=str(response.url))
    if isinstance(model, Contract):
        a, b, number = model.customer_inn, model.supplier_inn, model.contract_id
    else:
        a, b, number = model.plaintiff_inn, model.respondent_inn, model.case_number
    return model.model_copy(update={**audit, 'verified': bool(a and b and a != b and number
                                                              and valid_inn(a) and valid_inn(b))})


def mutual(record, targets):
    return (record.customer_inn in targets and record.supplier_inn in targets
            and record.customer_inn != record.supplier_inn)


def parse_zakupki_card(html, url):
    """Read one primary card; require explicitly scoped participant requisites."""
    soup = BeautifulSoup(html, 'html.parser')
    base = parse_contract_html(html)
    def parties(role):
        nodes = soup.select(f'[data-role="{role}"], #{role}, .{role}')
        if not nodes:
            patterns = (r'(?:информация|сведения).*заказчик',) if role == 'customer' else (r'(?:информация|сведения).*поставщик',)
            for heading in soup.select('h2, h3, .blockInfo__header, .section__header'):
                if any(re.search(p, heading.get_text(' ', strip=True), re.I) for p in patterns):
                    section = heading.find_parent(['section']) or heading.find_parent(class_='blockInfo')
                    if section:
                        nodes.append(section)
        result = []
        for node in nodes:
            values = []
            for label in node.select('tr, dt, .label, .field-label, .blockInfo__title, .section__title'):
                v = labelled(BeautifulSoup(str(label.parent), 'html.parser'), 'ИНН')
                if v and v not in values:
                    values.append(v)
            if len(values) == 1:
                result.append((tax_id(values[0]), clean(labelled(node, 'Полное наименование', 'Наименование'))))
        return list(dict.fromkeys(result))
    customers, suppliers = parties('customer'), parties('supplier')
    if len(customers) > 1:
        raise SourceChanged('Ambiguous EIS customer block')
    customer_inn, customer_name = customers[0] if customers else (base.customer_inn, base.customer_name)
    if not suppliers:
        suppliers = [(base.supplier_inn, base.supplier_name)]
    registry = parse_qs(urlparse(url).query)
    contract_id = base.contract_id or first(registry, 'reestrNumber', 'regNumber')
    if isinstance(contract_id, list):
        contract_id = contract_id[0] if len(contract_id) == 1 else None
    result = []
    for supplier_inn, supplier_name in suppliers:
        result.append(Contract(customer_inn=customer_inn, customer_name=customer_name,
            supplier_inn=supplier_inn, supplier_name=supplier_name, contract_id=contract_id,
            contract_date=base.contract_date, price_rub=base.price_rub, subject_text=base.subject_text))
    return result


def execution_evidence(html, record, url):
    soup = BeautifulSoup(html, 'html.parser')
    values = dict(stage_text=clean(labelled(soup, 'Этап исполнения', 'Статус исполнения', 'Статус контракта')),
                  acceptance_text=clean(labelled(soup, 'Сведения о приемке', 'Документ о приемке')),
                  payment_text=clean(labelled(soup, 'Сведения об оплате', 'Оплачено')))
    if not any(v is not None for v in values.values()):
        return None
    return ExecutionEvidence(customer_inn=record.customer_inn, supplier_inn=record.supplier_inn,
                             contract_id=record.contract_id, source_url=url, **values)


async def collect_zakupki(client, inn, role, pages, execution_sink=None):
    """44-FZ and 223-FZ searches; roles verified from primary card requisites."""
    records, seen_details = [], set()
    for endpoint in ('contract', 'contractfz223'):
        page, hashes = 1, set()
        url = f'https://zakupki.gov.ru/epz/{endpoint}/search/results.html'
        while True:
            response = await client.request('GET', url, params={'searchString': inn, 'pageNumber': page,
                'recordsPerPage': '_10', 'searchResultView': 'GENERAL', 'morphology': 'on'})
            soup = BeautifulSoup(response.text, 'html.parser')
            digest = hashlib.sha256(response.content).hexdigest()
            if digest in hashes:
                raise SourceChanged('Repeated EIS search page')
            hashes.add(digest)
            links = {urljoin(str(response.url), a['href']) for a in soup.select('a[href]')
                     if f'/epz/{endpoint}/card/' in a['href'] and
                     any(k in parse_qs(urlparse(a['href']).query) for k in ('reestrNumber', 'regNumber'))}
            if not links:
                if re.search(r'по вашему запросу ничего не найдено|поиск не дал результатов|записей не найдено', soup.get_text(' ', strip=True), re.I):
                    break
                raise SourceChanged('EIS page has no recognizable cards or explicit empty result')
            for detail_url in sorted(links):
                if detail_url in seen_details:
                    continue
                seen_details.add(detail_url)
                detail = await client.request('GET', detail_url)
                parsed = parse_zakupki_card(detail.text, str(detail.url))
                # A recognized search hit must not silently lose its party requisites.
                if not any(r.customer_inn and r.supplier_inn for r in parsed):
                    raise SourceChanged('EIS card participant requisites unavailable')
                matching = [annotate(r, detail) for r in parsed if getattr(r, f'{role}_inn') == inn]
                records.extend(matching)
                if execution_sink is not None and matching:
                    detail_soup = BeautifulSoup(detail.text, 'html.parser')
                    execution_pages = [(str(detail.url), detail.text)]
                    for a in detail_soup.select('a[href]'):
                        target = urljoin(str(detail.url), a['href'])
                        if '/card/' in urlparse(target).path and 'execution' in urlparse(target).path.lower():
                            response_execution = await client.request('GET', target)
                            execution_pages.append((str(response_execution.url), response_execution.text))
                    for record in matching:
                        for evidence_url, evidence_html in execution_pages:
                            evidence = execution_evidence(evidence_html, record, evidence_url)
                            if evidence:
                                execution_sink.append(evidence)
            next_link = soup.select_one('a[rel="next"], .pagination__next a[href], a.pagination__next[href]')
            if next_link is None:
                # Detect numbered later pages when the frontend has no explicit next link.
                later = [a for a in soup.select('a[href]') if any(v.isdigit() and int(v) > page
                         for v in parse_qs(urlparse(a['href']).query).get('pageNumber', []))]
                next_link = later[0] if later else None
            if next_link is None:
                if soup.select_one('.pagination, .pages') or len(links) < 10:
                    break
                raise SourceChanged('EIS pagination not recognizable; completeness unknown')
            if page >= pages:
                raise SourceChanged('EIS page limit reached; collection incomplete')
            page += 1
    return records


async def collect_clearspending(client, inn, role, pages):
    """Role filters verified against the current public frontend; recheck locally."""
    records, hashes = [], set()
    for page in range(1, pages + 1):
        response = await client.request('GET', 'https://clearspending.ru/api/gw/filtered-contracts/',
                                        params={('customer_inn' if role == 'customer' else 'supplier_inns'): inn,
                                                'page': page, 'page_size': 50})
        payload = json.loads(response.text, parse_float=Decimal)
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), list) or not isinstance(payload.get('total_pages'), int):
            raise SourceChanged('Clearspending response schema changed')
        digest = hashlib.sha256(response.content).hexdigest()
        if digest in hashes:
            raise SourceChanged('Repeated Clearspending page')
        hashes.add(digest)
        for row in payload['data']:
            for record in normalize_contract(row):
                if getattr(record, f'{role}_inn') == inn:
                    records.append(annotate(record, response))
        if page >= payload['total_pages']:
            return records
    raise SourceChanged('Clearspending page limit reached; collection incomplete')


async def collect_checko(client, inn, role, pages, starts=None):
    """Discover actual role links; explicit starts override discovery when layout changes."""
    if starts is None:
        response = await client.request('GET', 'https://checko.ru/search', params={'query': inn})
        soup = BeautifulSoup(response.text, 'html.parser')
        visible = soup.get_text(' ', strip=True)
        if 'мы не смогли ничего найти по вашему запросу' in visible:
            raise ProfileNotFound('Checko explicitly reports no profile for the supplied INN')
        if soup.title and re.search(r'ИНН\s+' + re.escape(inn) + r'\b', soup.title.get_text()):
            # Search redirects straight to a profile when there is one exact match.
            profile_url = str(response.url)
        else:
            profiles = {urljoin(str(response.url), a['href']) for a in soup.select('main a[href*="/company/"]')
                        if '/company/select' not in a['href']}
            if len(profiles) != 1:
                raise SourceChanged('Checko profile is ambiguous; provide --checko-sections')
            profile_url = profiles.pop()
            response = await client.request('GET', profile_url)
            soup = BeautifulSoup(response.text, 'html.parser')
            if not soup.title or not re.search(r'ИНН\s+' + re.escape(inn) + r'\b', soup.title.get_text()):
                raise SourceChanged('Checko profile INN could not be verified')
        starts = [urljoin(profile_url, a['href']) for a in soup.select('#contracts a[href]')
                  if f'role={role}' in a['href'] and '/contracts/' in a['href']]
        if not starts:
            raise SourceChanged('Checko role section not found; provide --checko-sections')
    if not isinstance(starts, list) or not starts or any(not isinstance(v, str) for v in starts):
        raise SourceChanged('Checko role requires a nonempty URL list')
    queue = list(starts)
    visited, records = set(), []
    while queue:
        url = queue.pop(0)
        if url in visited:
            continue
        if len(visited) >= pages:
            raise SourceChanged('Checko page/detail limit reached; collection incomplete')
        if urlparse(url).hostname not in ('checko.ru', 'www.checko.ru'):
            raise SourceChanged('Unexpected Checko link host')
        visited.add(url)
        response = await client.request('GET', url)
        soup = BeautifulSoup(response.text, 'html.parser')
        record = parse_contract_html(response.text)
        if getattr(record, f'{role}_inn') == inn:
            records.append(annotate(record, response))
        links = []
        for a in soup.select('a[href]'):
            href = a['href']
            if ('contract' in href.lower() or a.get('rel') and 'next' in a['rel']
                    or a.get_text(' ', strip=True).lower() in ('следующая', 'далее', '›')):
                target = urljoin(str(response.url), href)
                if target != str(response.url) and urlparse(target).hostname in ('checko.ru', 'www.checko.ru'):
                    links.append(target)
        queue.extend(v for v in links if v not in visited and v not in queue)
        if not records and not links and not re.search(r'нет контрактов|контракты отсутствуют', soup.get_text(' ', strip=True), re.I):
            raise SourceChanged('No recognizable contracts or explicit empty section')
    return records


async def kad_index(client, inn, pages):
    found, hashes = {}, set()
    for page in range(1, pages + 1):
        response = await client.request('POST', 'https://kad.arbitr.ru/Kad/SearchInstances',
            json={'Page': page, 'Count': 25, 'Sides': [{'Name': inn, 'Type': -1, 'ExactMatch': True}],
                  'Judges': [], 'Courts': [], 'CaseNumbers': [], 'DateFrom': None, 'DateTo': None,
                  'WithVKSInstances': False}, headers={'Referer': 'https://kad.arbitr.ru/', 'x-date-format': 'iso'})
        soup = BeautifulSoup(response.text, 'html.parser')
        total = soup.select_one('#documentsTotalCount')
        if total is None or not str(total.get('value', '')).isdigit():
            raise SourceChanged('KAD result count missing; possible challenge/layout change')
        digest = hashlib.sha256(response.content).hexdigest()
        if digest in hashes:
            raise SourceChanged('Repeated KAD page')
        hashes.add(digest)
        for tr in soup.select('tr'):
            a = tr.select_one('a[href*="/Card/"]')
            if a:
                node = tr.select_one('td.num div[title] span, .date')
                found[urljoin('https://kad.arbitr.ru', a['href'])] = (
                    clean(a.get_text(' ', strip=True)), parse_date(node.get_text(strip=True)) if node else None)
        if page * 25 >= int(total['value']):
            return found
    raise SourceChanged('KAD page limit reached; search incomplete')


def court_evidence(text):
    # Require explicit claim labels; unrelated money mentions are ignored.
    amounts = {money(m.group(1)) for m in re.finditer(
        r'(?:сумма (?:исковых )?требований|цена иска)\s*[:—-]?\s*(\d[\d\s\u00a0]*(?:[.,]\d{1,2})?)\s*(?:руб|₽)', text, re.I)}
    snippets = [s.strip() for s in re.split(r'(?<=[.!?])\s+|\n+', text)
                if re.search(r'недопостав\w*|наруш\w*\s+(?:\w+\s+){0,3}(?:срок\w*|услов\w*)\s+(?:\w+\s+){0,3}постав\w*|просроч\w*\s+постав\w*|срыв\w*\s+срок\w*|(?:ненадлежащ\w*|несоответств\w*|отклонени\w*|низк\w*)\s+(?:\w+\s+){0,4}качеств\w*|качеств\w*\s+(?:\w+\s+){0,4}(?:сырь\w*|материал\w*)', s, re.I)]
    return (next(iter(amounts)) if len(amounts) == 1 else None,
            '\n'.join(dict.fromkeys(snippets)) or None)


async def check_arbitration_pairs(client, pairs, *, pages=100, pdfs=False):
    """Intersect INN searches, then verify exact INNs on the case card."""
    pairs = sorted({tuple(sorted(pair)) for pair in pairs})
    for pair in pairs:
        if len(pair) != 2 or any(tax_id(i) is None for i in pair) or pair[0] == pair[1]:
            raise ValueError('Pairs require two distinct valid INNs')
    indices = {}
    for inn in sorted({i for pair in pairs for i in pair}):
        tax_id(inn)
        indices[inn] = await kad_index(client, inn, pages)
    cases, cache = [], {}
    for customer, supplier in pairs:
        for url in sorted(indices[customer].keys() & indices[supplier].keys()):
            if url not in cache:
                response = await client.request('GET', url)
                soup = BeautifulSoup(response.text, 'html.parser')
                # Exclude page-wide navigation; only a published participant section.
                section = soup.select_one('#case-participants, .case-participants, [data-role="participants"]')
                if section is None:
                    raise SourceChanged('KAD participants section unavailable; pair not confirmed')
                participant_inns = set(re.findall(r'(?<!\d)(?:\d{12}|\d{10})(?!\d)', section.get_text(' ', strip=True)))
                text = soup.get_text('\n', strip=True)
                evidence_urls = [url]
                if pdfs:
                    from pypdf import PdfReader
                    import io
                    for a in soup.select('a[href]'):
                        pdf_url = urljoin(url, a['href'])
                        if urlparse(pdf_url).path.lower().endswith('.pdf'):
                            client.allowed(pdf_url)
                            if urlparse(pdf_url).hostname not in ('kad.arbitr.ru', 'ras.arbitr.ru'):
                                continue
                            pdf = await client.request('GET', pdf_url)
                            if not pdf.content.startswith(b'%PDF-'):
                                raise SourceChanged('Published PDF link returned non-PDF')
                            def extract(content):
                                return '\n'.join(p.extract_text() or '' for p in PdfReader(io.BytesIO(content)).pages)
                            text += '\n' + await asyncio.to_thread(extract, pdf.content)
                            evidence_urls.append(pdf_url)
                roles = {}
                for key in ('plaintiff', 'respondent'):
                    role_node = section.select_one(f'[data-role="{key}"], .{key}, #{key}')
                    roles[key] = set(re.findall(r'(?<!\d)(?:\d{12}|\d{10})(?!\d)', role_node.get_text(' ', strip=True))) if role_node else set()
                cache[url] = participant_inns, text, evidence_urls, roles, response
            inns, text, urls, roles, card_response = cache[url]
            if customer not in inns or supplier not in inns:
                continue
            directions = [(a, b) for a, b in ((customer, supplier), (supplier, customer))
                          if a in roles['plaintiff'] and b in roles['respondent']]
            if len(directions) != 1:
                raise SourceChanged('KAD opposing procedural roles not confirmed')
            plaintiff, respondent = directions[0]
            amount, dispute = court_evidence(text)
            number, registered = indices[customer][url]
            cases.append(annotate(CourtCase(party_a_inn=customer, party_b_inn=supplier,
                plaintiff_inn=plaintiff, respondent_inn=respondent, case_number=number,
                registration_date=registered, claim_amount_rub=amount, dispute_text=dispute,
                source_url=url, evidence_urls=urls,
                dispute_evidence='keyword_candidate' if dispute else 'not_available'), card_response))
    return cases


def atomic_ndjson(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            for record in records:
                out.write(json_line(record))
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


async def run(args):
    targets = set(args.inns)
    for inn in targets:
        tax_id(inn)
    proxies = [v.strip() for v in args.proxies.read_text().splitlines() if v.strip() and not v.lstrip().startswith('#')]
    sections = json.loads(args.checko_sections.read_text()) if args.checko_sections else {}
    report, evidence, records, execution = [], [], {}, []
    failure_details = {}
    async with Transport(proxies, concurrency=args.concurrency, interval=args.interval,
                         raw_dir=args.output / 'raw') as client:
        async def job(source, inn, role):
            try:
                if source == 'checko':
                    result = await collect_checko(client, inn, role, args.max_pages, sections.get(inn, {}).get(role))
                elif source == 'zakupki':
                    local_execution = []
                    result = await collect_zakupki(client, inn, role, args.max_pages, local_execution)
                    execution.extend(e for e in local_execution if e.customer_inn in targets and e.supplier_inn in targets
                                     and e.customer_inn != e.supplier_inn)
                else:
                    result = await collect_clearspending(client, inn, role, args.max_pages)
                return source, inn, role, result, None
            except (ValueError, RuntimeError, httpx.HTTPError) as exc:
                failure_details[(source, inn, role)] = {'http_status': exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None}
                if isinstance(exc, RuntimeError):
                    failure_details[(source, inn, role)]['reason'] = str(exc)
                return source, inn, role, [], type(exc).__name__
        tasks = [job(source, inn, role) for source in args.sources for inn in sorted(targets)
                 for role in ('customer', 'supplier')]
        for future in asyncio.as_completed(tasks):
            source, inn, role, result, error = await future
            report.append({'source': source, 'inn': inn, 'role': role,
                           'status': ('profile_not_found' if error == 'ProfileNotFound' else 'failed') if error else 'complete', 'error_type': error,
                           'records_before_mutual_filter': len(result),
                           **failure_details.get((source, inn, role), {})})
            LOG.info('%s %s %s: %s', source, inn, role, error or f'{len(result)} records')
            for record in result:
                if mutual(record, targets):
                    # Preserve conflicting versions instead of silently overwriting them.
                    line = json_line(record)
                    records[line] = record
                    evidence.append({'record_sha256': hashlib.sha256(line.encode()).hexdigest(),
                                     'source': source, 'search_inn': inn, 'role': role})
        await asyncio.to_thread(atomic_ndjson, args.output / 'contracts.ndjson', list(records.values()))
        await asyncio.to_thread(atomic_ndjson, args.output / 'execution.ndjson',
                                list({json_line(e): e for e in execution}.values()))
        if args.kad:
            # Court queries cover every target pair, including those with no discovered contract.
            pairs = set(itertools.combinations(sorted(targets), 2))
            if args.pairs:
                pairs.update(tuple(p) for p in json.loads(args.pairs.read_text()))
            try:
                cases = await check_arbitration_pairs(client, pairs, pages=args.max_pages, pdfs=args.kad_pdfs)
                await asyncio.to_thread(atomic_ndjson, args.output / 'court_cases.ndjson', cases)
                report.append({'source': 'kad', 'status': 'complete', 'requested_pairs': len(pairs), 'cases': len(cases)})
            except (ValueError, RuntimeError, httpx.HTTPError) as exc:
                report.append({'source': 'kad', 'status': 'failed', 'requested_pairs': len(pairs),
                               'error_type': type(exc).__name__,
                               'http_status': exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None})
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'coverage.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    (args.output / 'provenance.ndjson').write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in evidence))
    return 2 if any(r['status'] in ('failed', 'profile_not_found') for r in report) else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inns', nargs='+', default=TARGETS)
    parser.add_argument('--proxies', type=Path)
    parser.add_argument('--local-contracts', type=Path, help='Offline ClearSpending contracts.csv; target customer OR supplier')
    parser.add_argument('--kad-html', nargs='+', type=Path, help='Saved KAD case HTML for offline mode with --kad')
    parser.add_argument('--output', type=Path, default=Path('outputs/contract_links'))
    parser.add_argument('--sources', nargs='+', choices=['zakupki', 'checko', 'clearspending'], default=['zakupki', 'checko', 'clearspending'])
    parser.add_argument('--checko-sections', type=Path)
    parser.add_argument('--max-pages', type=int, default=100)
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--interval', type=float, default=2)
    parser.add_argument('--kad', action='store_true')
    parser.add_argument('--kad-pdfs', action='store_true')
    parser.add_argument('--pairs', type=Path, help='JSON array of additional [party_a_inn, party_b_inn] pairs')
    args = parser.parse_args()
    if args.kad_html and (not args.kad or not args.local_contracts):
        parser.error('--kad-html requires --kad and --local-contracts')
    if not args.local_contracts and not args.proxies:
        parser.error('Online collection requires --proxies')
    if args.local_contracts and args.kad_pdfs:
        parser.error('Offline mode accepts saved case HTML; --kad-pdfs is online only')
    if args.kad_pdfs:
        try:
            import pypdf
        except ImportError:
            parser.error('--kad-pdfs requires pypdf; install the optional dependency')
    if args.concurrency < 1 or args.max_pages < 1 or args.interval < 0:
        parser.error('concurrency/max-pages must be positive; interval must be nonnegative')
    if (args.kad_pdfs or args.pairs) and not args.kad:
        parser.error('--kad-pdfs/--pairs require --kad')
    if args.output.exists() and any(args.output.iterdir()):
        parser.error('Use a new empty output directory to avoid mixing runs')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        if args.local_contracts:
            from local_contract_evidence import run_local
            raise SystemExit(run_local(args))
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        raise SystemExit(130)
    except (OSError, ValueError, RuntimeError) as exc:
        LOG.error('Startup/run failed: %s', type(exc).__name__)
        raise SystemExit(2)


if __name__ == '__main__':
    main()
