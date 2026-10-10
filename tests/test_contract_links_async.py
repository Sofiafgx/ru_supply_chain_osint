import asyncio
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import httpx
from pydantic import ValidationError

from contract_links_async import (AccessBlocked, Contract, SourceChanged, Transport,
    atomic_ndjson, check_arbitration_pairs, collect_clearspending, collect_zakupki, court_evidence, execution_evidence, json_line, money, mutual, parse_zakupki_card,
    normalize_contract, parse_contract_html)


class DataTests(unittest.TestCase):
    def record(self, **changes):
        values = dict(customer_inn='1646019864', supplier_inn='2128700223', customer_name=None,
                      supplier_name=None, contract_id=None, contract_date=None,
                      price_rub=None, subject_text=None)
        values.update(changes)
        return Contract(**values)

    def test_null_fields_and_exact_numeric_decimal(self):
        line = json_line(self.record(price_rub=Decimal('1234567890123456.78')))
        row = json.loads(line, parse_float=Decimal)
        self.assertEqual(row['price_rub'], Decimal('1234567890123456.78'))
        self.assertIsNone(row['contract_date'])
        self.assertEqual(len(row), 8)

    def test_inn_is_not_coerced_from_integer(self):
        with self.assertRaises(ValidationError):
            self.record(customer_inn=1646019864)

    def test_mutual_only_targets_and_no_self_contract(self):
        targets = {'1646019864', '2128700223'}
        self.assertTrue(mutual(self.record(), targets))
        self.assertFalse(mutual(self.record(supplier_inn='1804002498'), targets))
        self.assertFalse(mutual(self.record(supplier_inn='1646019864'), targets))
        self.assertFalse(mutual(self.record(supplier_inn=None), targets))

    def test_multiple_supplier_names_not_assumed_aligned(self):
        rows = normalize_contract({'customer_inn': '1646019864',
            'supplier_inns': ['2128700223', '1804002498'], 'supplier_names': ['A', 'B']})
        self.assertEqual([r.supplier_name for r in rows], [None, None])

    def test_missing_currency_not_assumed_rub(self):
        self.assertIsNone(normalize_contract({'amount': 42, 'supplier_inns': ['2128700223']})[0].price_rub)

    def test_contract_html_does_not_take_unlabelled_inns(self):
        record = parse_contract_html('<p>1646019864 2128700223</p><table><tr><td>Номер контракта</td><td>42</td></tr></table>')
        self.assertIsNone(record.customer_inn)
        self.assertIsNone(record.supplier_inn)
        self.assertEqual(record.contract_id, '42')

    def test_claim_amount_requires_explicit_label_and_dispute_evidence(self):
        self.assertEqual(court_evidence('Госпошлина 1000 руб. Договор на 25000 руб.'), (None, None))
        amount, text = court_evidence('Цена иска: 12 345,67 руб. Просрочка поставки товара.')
        self.assertEqual(amount, Decimal('12345.67'))
        self.assertIn('Просрочка поставки', text)
        self.assertIsNone(court_evidence('Цена иска: 10 руб. Цена иска: 20 руб.')[0])

    def test_invalid_money_rejected(self):
        for value in (True, '-1', 'NaN', 'not money'):
            with self.assertRaises(ValueError):
                money(value)

    def test_atomic_output_valid_ndjson(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'contracts.ndjson'
            atomic_ndjson(out, [self.record(), self.record(contract_id='two')])
            self.assertEqual(len([json.loads(s) for s in out.read_text().splitlines()]), 2)

    def test_quality_and_delivery_terms_are_literal_claim_snippets(self):
        for text in ('Поставлено сырье ненадлежащего качества.',
                     'Нарушение условий поставки сырья.',
                     'Отклонения по качеству сырья установлены экспертизой.'):
            self.assertEqual(court_evidence(text)[1], text)
            self.assertIsNone(court_evidence(text)[0])

    def test_eis_multiple_supplier_objects_and_primary_registry_identifier(self):
        html = ('<section data-role="customer"><dl><dt>ИНН</dt><dd>1646019864</dd>'
                '<dt>Полное наименование</dt><dd>Customer</dd></dl></section>'
                '<section data-role="supplier"><dl><dt>ИНН</dt><dd>2128700223</dd>'
                '<dt>Полное наименование</dt><dd>Supplier A</dd></dl></section>'
                '<section data-role="supplier"><dl><dt>ИНН</dt><dd>1804002498</dd>'
                '<dt>Полное наименование</dt><dd>Supplier B</dd></dl></section>')
        rows = parse_zakupki_card(html, 'https://zakupki.gov.ru/epz/contract/card/common-info.html?reestrNumber=42')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].customer_name, 'Customer')
        self.assertEqual([r.supplier_name for r in rows], ['Supplier A', 'Supplier B'])
        self.assertEqual([r.contract_id for r in rows], ['42', '42'])
        self.assertIsNone(rows[0].price_rub)

    def test_execution_never_inferred_from_signed_contract(self):
        self.assertIsNone(execution_evidence('<p>Контракт заключен</p>', self.record(), 'https://zakupki.gov.ru/'))
        evidence = execution_evidence('<dl><dt>Сведения о приемке</dt><dd>Акт № 12 от 01.10.2026</dd></dl>',
                                      self.record(), 'https://zakupki.gov.ru/')
        self.assertEqual(evidence.acceptance_text, 'Акт № 12 от 01.10.2026')
        self.assertIsNone(evidence.payment_text)
        self.assertIsNone(evidence.stage_text)


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_rotation_and_redirect_validation(self):
        seen, options = [], []
        def factory(**kwargs):
            options.append(kwargs.copy())
            proxy = str(kwargs['proxy'].url) if isinstance(kwargs['proxy'], httpx.Proxy) else kwargs['proxy']
            def handle(request):
                seen.append(proxy)
                if request.url.path == '/redirect':
                    return httpx.Response(302, headers={'location': 'https://attacker.ru/'})
                return httpx.Response(200, json={'ok': True})
            return httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False)
        async with Transport(['http://one:8080', 'https://two:443'], interval=0, factory=factory) as client:
            await client.request('GET', 'https://checko.ru/one')
            await client.request('GET', 'https://checko.ru/two')
            with self.assertRaises(ValueError):
                await client.request('GET', 'https://checko.ru/redirect')
        self.assertEqual(seen, ['http://one:8080', 'https://two', 'http://one:8080'])
        self.assertTrue(all(o['verify'] is False and o['trust_env'] is False for o in options))

    async def test_no_direct_fallback(self):
        with self.assertRaises(ValueError):
            Transport([])

    async def test_challenge_is_error_not_empty_result(self):
        def factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(
                200, text='<h1>Oh noes!</h1>Access Denied', headers={'content-type': 'text/html'})))
        async with Transport(['http://one:8080'], interval=0, factory=factory) as client:
            with self.assertRaises(AccessBlocked):
                await client.request('GET', 'https://clearspending.ru/docs')

    async def test_clearspending_pagination_role_filter_and_limit(self):
        class Client:
            async def request(self, method, url, **kwargs):
                page = kwargs['params']['page']
                return httpx.Response(200, request=httpx.Request(method, url, params=kwargs.get('params')), json={'total_pages': 2, 'data': [{
                    'customer_inn': '1646019864', 'supplier_inns': ['2128700223'],
                    'regnum': str(page)}]})
        records = await collect_clearspending(Client(), '1646019864', 'customer', 3)
        self.assertEqual(len(records), 2)
        self.assertTrue(all(r.input_sha256 and r.collected_at and r.source_url for r in records))
        self.assertEqual(await collect_clearspending(Client(), '1646019864', 'supplier', 3), [])
        with self.assertRaises(SourceChanged):
            await collect_clearspending(Client(), '1646019864', 'customer', 1)

    async def test_kad_confirms_both_inns_and_keeps_unknown_fields_null(self):
        class Client:
            async def request(self, method, url, **kwargs):
                if method == 'POST':
                    return httpx.Response(200, text='<input id="documentsTotalCount" value="1">'
                        '<table><tr><td class="num"><a href="/Card/shared">А40-12/2026</a>'
                        '<span class="date">01.10.2026</span></td></tr></table>')
                return httpx.Response(200, request=httpx.Request(method, url), text='<section id="case-participants">'
                    '<div data-role="plaintiff">ИНН 1646019864</div>'
                    '<div data-role="respondent">ИНН 2128700223</div></section>')
        cases = await check_arbitration_pairs(Client(), [('1646019864', '2128700223')])
        self.assertEqual(len(cases), 1)
        self.assertIsNone(cases[0].claim_amount_rub)
        self.assertIsNone(cases[0].dispute_text)
        self.assertEqual(cases[0].registration_date.isoformat(), '2026-10-01')

    async def test_kad_never_confirms_pair_from_unrelated_page_inns(self):
        class Client:
            async def request(self, method, url, **kwargs):
                if method == 'POST':
                    return httpx.Response(200, text='<input id="documentsTotalCount" value="1">'
                        '<table><tr><td><a href="/Card/shared">А40-12/2026</a></td></tr></table>')
                return httpx.Response(200, text='<section id="case-participants">ИНН 1646019864</section>'
                    '<footer>2128700223</footer>')
        cases = await check_arbitration_pairs(Client(), [('1646019864', '2128700223')])
        self.assertEqual(cases, [])

    async def test_kad_shared_case_without_opposing_roles_is_rejected(self):
        class Client:
            async def request(self, method, url, **kwargs):
                if method == 'POST':
                    return httpx.Response(200, text='<input id="documentsTotalCount" value="1">'
                        '<table><tr><td><a href="/Card/shared">А40-12/2026</a></td></tr></table>')
                return httpx.Response(200, request=httpx.Request(method, url), text='<section id="case-participants">'
                    '<div data-role="plaintiff">1646019864 2128700223</div></section>')
        with self.assertRaises(SourceChanged):
            await check_arbitration_pairs(Client(), [('1646019864', '2128700223')])

    async def test_eis_both_registers_and_both_roles(self):
        class Client:
            def __init__(self):
                self.searches = []
            async def request(self, method, url, **kwargs):
                if '/search/' in url:
                    self.searches.append(url)
                    endpoint = 'contractfz223' if 'contractfz223' in url else 'contract'
                    return httpx.Response(200, text=f'<a href="/epz/{endpoint}/card/common-info.html?reestrNumber={endpoint}">Contract</a>',
                                          request=httpx.Request(method, url))
                return httpx.Response(200, text='<table><tr><td>ИНН заказчика</td><td>1646019864</td></tr>'
                    '<tr><td>ИНН поставщика</td><td>2128700223</td></tr></table>', request=httpx.Request(method, url))
        client = Client()
        rows = await collect_zakupki(client, '1646019864', 'customer', 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(client.searches), 2)
        self.assertEqual(await collect_zakupki(Client(), '1646019864', 'supplier', 2), [])

    async def test_eis_missing_cards_are_not_assumed_empty(self):
        class Client:
            async def request(self, method, url, **kwargs):
                return httpx.Response(200, text='<h1>Unknown layout</h1>', request=httpx.Request(method, url))
        with self.assertRaises(SourceChanged):
            await collect_zakupki(Client(), '1646019864', 'customer', 2)

    async def test_saved_clearspending_response(self):
        path = Path(__file__).resolve().parents[1] / 'data/collection_20261008/raw/clearspending/73e8818938bde3a95962c4d4.json'
        if not path.exists():
            self.skipTest('Local snapshot unavailable')
        payload = json.loads(path.read_text(), parse_float=Decimal)
        rows = [r for row in payload['data'] for r in normalize_contract(row)]
        self.assertTrue(rows)
        self.assertTrue(all(r.contract_date and r.contract_id for r in rows))


if __name__ == '__main__':
    unittest.main()
